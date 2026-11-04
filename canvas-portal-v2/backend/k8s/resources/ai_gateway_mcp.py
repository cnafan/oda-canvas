"""Add, change and remove MCP servers in agentgateway's config (mcp.targets).

Unlike a model, a bad target still reloads cleanly. With the default failureMode, it then
fails every MCP request through the gateway, so each change is also checked by listing tools.
"""

import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from kubernetes import client as k8s

from config import get_settings
from k8s.resources import ai_gateway_config as config_file
from k8s.resources import ai_gateways
from k8s.resources.ai_gateway_config import as_dict
from k8s.resources.ai_gateways import GatewayRequestRefused, parse_config_dump

_TRANSPORTS = ("mcp", "sse", "openapi", "stdio")
_ADDRESS_FIELDS = ("host", "port", "path", "backend")
_TOKEN_PATH = ("policies", "backendAuth", "key", "file")
_FIELDS_THAT_KEEP_THE_UPSTREAM = {"name", "condition"}


def _entries(config: config_file.GatewayConfig) -> list[Any]:
    return config.entries("mcp", "targets")


def _transport(entry: dict[str, Any] | None) -> str | None:
    return next((name for name in _TRANSPORTS if name in as_dict(entry)), None)


def _has_token(entry: dict[str, Any] | None) -> bool:
    return set(as_dict(as_dict(as_dict(entry).get("policies")).get("backendAuth"))) == {"key"}


def _inline_schema(spec: dict[str, Any]) -> str | None:
    # The gateway expands the file's environment variables, so $ is stored as $$.
    schema = spec.get("schema")
    return schema.replace("$$", "$") if isinstance(schema, str) else None


def _view(entry: dict[str, Any]) -> dict[str, Any]:
    transport = _transport(entry)
    spec = as_dict(entry.get(transport))
    policies = dict(as_dict(entry.get("policies")))
    if _has_token(entry):
        del policies["backendAuth"]
    return {
        "name": entry.get("name"),
        "transport": transport,
        **{field: spec.get(field) for field in _ADDRESS_FIELDS},
        "schema": _inline_schema(spec),
        "schemaKept": "schema" in spec and not isinstance(spec["schema"], str),
        "command": spec.get("cmd") if transport == "stdio" else None,
        "condition": entry.get("condition"),
        "tokenSet": _has_token(entry),
        "policies": policies,
        "revision": config_file.revision(entry),
    }


def list_mcp_targets(core: k8s.CoreV1Api | None = None) -> dict[str, Any]:
    if core is None and not config_file.config_is_changeable():
        return {"configurable": False, "targets": []}
    config = config_file.read_config(core or config_file.core_api())
    return {"configurable": True, "targets": [_view(entry) for entry in _entries(config) if isinstance(entry, dict)]}


def _token_file_path(entry: dict[str, Any] | None) -> str | None:
    key = as_dict(as_dict(as_dict(as_dict(entry).get("policies")).get("backendAuth")).get("key"))
    return key["file"] if isinstance(key.get("file"), str) else None


def _build_spec(request: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    transport = request["transport"]
    same_transport = _transport(existing) == transport
    old_spec = as_dict(as_dict(existing).get(transport)) if same_transport else {}
    if transport == "stdio":
        # It runs a process inside the gateway pod, with the gateway's environment and secrets.
        if not same_transport:
            raise GatewayRequestRefused(422, "stdio servers can't be added from the portal.")
        return old_spec

    spec = {key: value for key, value in old_spec.items() if key not in (*_ADDRESS_FIELDS, "schema")}
    host = request["host"]
    if host:
        if "://" in host and urlsplit(host).scheme not in ("http", "https"):
            raise GatewayRequestRefused(422, "The host must start with http:// or https://, or have no scheme.")
        config_file.refuse_gateway_local_host(host)
        spec["host"] = host
        if request["port"] is not None:
            spec["port"] = request["port"]
    elif old_spec.get("backend"):
        spec["backend"] = old_spec["backend"]
    else:
        raise GatewayRequestRefused(422, "A host is required.")
    if request["path"]:
        spec["path"] = request["path"]

    if transport == "openapi":
        schema = request["schema"]
        if schema and schema != _inline_schema(old_spec):
            spec["schema"] = schema.replace("$", "$$")
        elif "schema" in old_spec:
            spec["schema"] = old_spec["schema"]
        else:
            raise GatewayRequestRefused(422, "An OpenAPI schema is required.")
    return spec


def _without_inline_schema(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """An inline schema's $ is escaped, so it isn't an environment variable."""
    transport = _transport(entry)
    if transport != "openapi" or not isinstance(as_dict(entry[transport]).get("schema"), str):
        return entry
    return {**entry, transport: {k: v for k, v in entry[transport].items() if k != "schema"}}


def _upstream_settings(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Everything that could change where a request goes, including settings added later."""
    return {key: value for key, value in as_dict(entry).items() if key not in _FIELDS_THAT_KEEP_THE_UPSTREAM}


def _build_entry(request: dict[str, Any], existing: dict[str, Any] | None, key_file: str | None) -> dict[str, Any]:
    transport = request["transport"]
    spec = _build_spec(request, existing)
    policies = dict(request["policies"])
    if "backendAuth" in policies and (key_file or (_has_token(existing) and not request["removeAuthToken"])):
        raise GatewayRequestRefused(422, "Set backendAuth or the auth token, not both.")
    if key_file:
        policies["backendAuth"] = {"key": {"file": config_file.key_file_path(key_file)}}
    elif _has_token(existing) and not request["removeAuthToken"]:
        policies["backendAuth"] = existing["policies"]["backendAuth"]

    entry: dict[str, Any] = {"name": request["name"]}
    if request["condition"]:
        entry["condition"] = request["condition"]
    if policies:
        entry["policies"] = policies
    entry[transport] = spec

    checked, old = _without_inline_schema(entry), _without_inline_schema(existing)
    allowed_file = (*_TOKEN_PATH, config_file.key_file_path(key_file)) if key_file else None
    config_file.refuse_new_secrets(checked, old, allowed_file)
    if existing is not None and _upstream_settings(entry) != _upstream_settings(existing):
        kept_token = _has_token(existing) and as_dict(entry.get("policies")).get("backendAuth") == existing["policies"]["backendAuth"]
        if kept_token:
            raise GatewayRequestRefused(
                422, "This changes where requests go, so enter its auth token again or remove it."
            )
        config_file.refuse_keeping_secrets_for_new_upstream(checked, old)
    return entry


def _target_names(dump: Any) -> set[str]:
    return {tool["name"] for tool in parse_config_dump(dump)["tools"]}


def _listing_problem(http: httpx.Client) -> str | None:
    url = get_settings().AGENTGATEWAY_MCP_URL
    if not url:
        return None
    ai_gateways._discovery_cache.clear()
    return ai_gateways._cached_discovery(http, url).get("discoveryError")


def _save(
    request: dict[str, Any], original_name: str | None, revision: str | None, core: k8s.CoreV1Api, http: httpx.Client
) -> dict[str, Any]:
    config = config_file.read_config(core)
    entries = _entries(config)
    existing = config_file.find_entry(entries, original_name) if original_name else None
    if original_name and existing is None:
        raise GatewayRequestRefused(404, "This MCP server isn't in the gateway config any more.")
    if existing is not None and config_file.revision(existing) != revision:
        raise GatewayRequestRefused(409, "This MCP server was changed after you opened it. Reopen it to see the changes.")
    other = config_file.find_entry(entries, request["name"])
    if other is not None and other is not existing:
        raise GatewayRequestRefused(409, "An MCP server with this name already exists.")

    new_key = (secrets.token_hex(8), request["authToken"]) if request["authToken"] else None
    entry = _build_entry(request, existing, new_key[0] if new_key else None)
    if entry == existing:
        return {"applied": True, "message": "Nothing changed."}
    before = config_file.ready_to_change(http)
    was_working = _listing_problem(http) is None

    if existing is None:
        entries.append(entry)
    else:
        entries[entries.index(existing)] = entry

    if existing is not None and request["name"] == original_name:
        applied = lambda dump: dump != before.dump
    else:
        applied = lambda dump: request["name"] in _target_names(dump)

    def verify() -> str | None:
        if not was_working:
            return None
        problem = _listing_problem(http)
        if problem:
            # A server that's slow to answer its first request shouldn't undo the change.
            time.sleep(config_file.LISTING_RETRY_SECONDS)
            problem = _listing_problem(http)
        if problem:
            return f"with this server, the gateway couldn't list MCP tools for any client ({problem}). Check its address and token."
        return None

    old_key_file = None
    if new_key or request["removeAuthToken"]:
        old_key_file = config_file.unused_portal_key(_token_file_path(existing), config.document)
    try:
        result = config_file.change_config(core, http, config, before, applied, new_key, old_key_file, verify)
    finally:
        ai_gateways._discovery_cache.clear()
    if not was_working:
        result["warning"] = "MCP tools were already unavailable before this change, so it couldn't be checked."
    return result


def save_mcp_target(
    request: dict[str, Any],
    original_name: str | None = None,
    revision: str | None = None,
    core: k8s.CoreV1Api | None = None,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    return config_file.run_change(lambda api, client: _save(request, original_name, revision, api, client), core, http)


def _remove(name: str, core: k8s.CoreV1Api, http: httpx.Client) -> dict[str, Any]:
    config = config_file.read_config(core)
    entries = _entries(config)
    existing = config_file.find_entry(entries, name)
    if existing is None:
        raise GatewayRequestRefused(404, "This MCP server isn't in the gateway config any more.")
    before = config_file.ready_to_change(http)
    entries.remove(existing)
    old_key = config_file.unused_portal_key(_token_file_path(existing), config.document)
    try:
        return config_file.change_config(
            core, http, config, before, lambda dump: name not in _target_names(dump), None, old_key
        )
    finally:
        ai_gateways._discovery_cache.clear()


def remove_mcp_target(name: str, core: k8s.CoreV1Api | None = None, http: httpx.Client | None = None) -> dict[str, Any]:
    return config_file.run_change(lambda api, client: _remove(name, api, client), core, http)
