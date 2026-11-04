"""Read the active agentgateway configuration for the AI Gateways page.

The source is agentgateway's /config_dump, a debug API with no stability
guarantee, so parsing is defensive. Only whitelisted fields leave this module.
"""

import json
import re
import socket
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from cachetools import TTLCache

from config import get_settings

MCP_ACCEPT = "application/json, text/event-stream"
MAX_MCP_RESPONSE_BYTES = 1024 * 1024

# Case-sensitive `[Tt]oken$`, so rate-limit fields such as maxTokens stay visible.
_SECRET_KEY = re.compile(
    r"[Ss]ecret|[Pp]assword|[Aa]pi[-_]?[Kk]ey|[Aa]uthorization|[Cc]ookie|[Cc]redential|[Tt]oken$|^key$"
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+"  # Authorization values
    r"|\bsk-[A-Za-z0-9_-]{8,}"  # Provider API keys
    r"|(?<=://)[^/@\s]+(?=@)"  # URL userinfo
    r"|^[^/@\s]+(?=@)"  # Userinfo on a host with no scheme
    r"|(?<=[?&]key=)[^&\s]+"  # Secret-like query parameters
    r"|(?<=[?&]token=)[^&\s]+"
    r"|(?<=[?&]secret=)[^&\s]+"
    r"|(?<=[?&]password=)[^&\s]+"
    r"|(?<=[?&]api_key=)[^&\s]+"
    r"|(?<=[?&]apikey=)[^&\s]+"
)

# Policies shown elsewhere on the page, or with nothing to show.
_NON_TRAFFIC_POLICIES = {"a2a", "budget"}
_POLICY_FIELDS = {
    "timeout": ("requestTimeout", "backendRequestTimeout"),
    "retry": ("attempts", "backoff", "codes"),
    "localRateLimit": ("type", "maxTokens", "tokensPerFill", "fillInterval"),
}

_discovery_cache: TTLCache = TTLCache(maxsize=1, ttl=30)
_discovery_lock = threading.Lock()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def sanitize(value: Any) -> Any:
    """Mask secret-like keys and values. Upstream redaction is only per field."""
    if isinstance(value, dict):
        return {k: "<redacted>" if _SECRET_KEY.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


def _format_match(match: Any) -> str:
    match = _as_dict(match)
    path = _as_dict(match.get("path"))
    if "exact" in path:
        text = str(path["exact"])
    elif "pathPrefix" in path:
        text = f"{str(path['pathPrefix']).rstrip('/')}/*"
    elif "regex" in path:
        text = f"regex {path['regex']}"
    else:
        text = "*"
    method = match.get("method")
    return f"{method} {text}" if isinstance(method, str) else text


def _backend_index(dump: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any], list[Any]]]:
    """Map backend name to (kind, body, inline policies)."""
    index = {}
    for entry in _as_list(dump.get("backends")):
        entry = _as_dict(entry)
        for kind, body in _as_dict(entry.get("backend")).items():
            body = _as_dict(body)
            name = body.get("name")
            if isinstance(name, str):
                index[name] = (kind, body, _as_list(entry.get("inlinePolicies")))
    return index


def _backend_ref(ref: Any) -> str:
    return ref.lstrip("/") if isinstance(ref, str) else ""


def _routes(dump: dict[str, Any], backends: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten routes, leaving out the gateway's own read path (internal backends only)."""
    routes = []
    for bind in _as_list(dump.get("binds")):
        for listener in _as_dict(_as_dict(bind).get("listeners")).values():
            for route in _as_dict(_as_dict(listener).get("routes")).values():
                route = _as_dict(route)
                refs = [_as_dict(b) for b in _as_list(route.get("backends"))]
                names = [_backend_ref(b.get("backend")) for b in refs]
                kinds = [backends.get(n, ("",))[0] for n in names]
                if kinds and all(kind == "internal" for kind in kinds):
                    continue
                routes.append({
                    "name": str(route.get("name") or route.get("key") or ""),
                    "matches": [_format_match(m) for m in _as_list(route.get("matches"))],
                    "refs": refs,
                    "backendNames": names,
                    "policies": _as_list(route.get("inlinePolicies")),
                })
    return routes


def _routes_to(routes: list[dict[str, Any]], names: set[str]) -> list[str]:
    matches: list[str] = []
    for route in routes:
        if names.intersection(route["backendNames"]):
            matches.extend(m for m in route["matches"] if m not in matches)
    return matches


def _parse_models(backends: dict[str, Any], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routers = [name for name, (kind, _, _) in backends.items() if kind == "llmRouter"]
    models = []
    for name, (kind, body, _) in backends.items():
        if kind != "ai":
            continue
        match = _routes_to(routes, {name, *routers})
        for group in _as_list(_as_dict(body.get("target")).get("providers")):
            group = _as_dict(group)
            for status in ("active", "rejected"):
                for model_name, item in _as_dict(group.get(status)).items():
                    endpoint = _as_dict(_as_dict(item).get("endpoint"))
                    provider = next(iter(_as_dict(endpoint.get("provider"))), "")
                    models.append({
                        "name": str(model_name),
                        "provider": provider,
                        "upstream": f"{endpoint.get('hostOverride') or ''}{endpoint.get('pathPrefix') or ''}",
                        "status": status,
                        "matches": match,
                    })
    return models


def _parse_tools(
    backends: dict[str, Any], routes: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any]]:
    tools = []
    for name, (kind, body, _) in backends.items():
        if kind != "mcp":
            continue
        target_config = _as_dict(body.get("target"))
        match = _routes_to(routes, {name})
        for target in _as_list(target_config.get("targets")):
            target = _as_dict(target)
            transport, spec = next(
                ((k, v) for k, v in target.items() if isinstance(v, dict)), ("", {})
            )
            ref = _backend_ref(_as_dict(spec.get("backend")).get("backend"))
            resolved = backends.get(ref)
            host = resolved[1].get("target") if resolved and resolved[0] == "host" else None
            if not isinstance(host, str):
                host = ""
                warnings.append(f"MCP target {target.get('name')} has no resolvable host")
            tools.append({
                "name": str(target.get("name") or ""),
                "transport": transport,
                "upstream": f"{host}{spec.get('path') or ''}",
                "stateful": bool(target_config.get("stateful")),
                "failureMode": str(target_config.get("failureMode") or ""),
                "matches": match,
            })
    return tools


def _has_a2a(policies: list[Any]) -> bool:
    return any("a2a" in _as_dict(policy) for policy in policies)


def _parse_agents(backends: dict[str, Any], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents = []
    for route in routes:
        for ref, name in zip(route["refs"], route["backendNames"]):
            resolved = backends.get(name, ("", {}, []))
            if not (
                _has_a2a(_as_list(ref.get("inlinePolicies")))
                or _has_a2a(route["policies"])
                or _has_a2a(resolved[2])
            ):
                continue
            target = resolved[1].get("target")
            agents.append({
                "name": route["name"],
                "matches": route["matches"],
                "backend": target if isinstance(target, str) else name,
            })
    return agents


def _policy_settings(policy_type: str, body: Any) -> list[dict[str, Any]]:
    fields = _POLICY_FIELDS.get(policy_type, ())
    items = body if isinstance(body, list) else [body]
    return [
        {field: item[field] for field in fields if field in item}
        for item in map(_as_dict, items)
    ]


def _parse_traffic(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for route in routes:
        for policy in route["policies"]:
            for policy_type, body in _as_dict(policy).items():
                if policy_type in _NON_TRAFFIC_POLICIES:
                    continue
                rows.extend(
                    {
                        "route": route["name"],
                        "matches": route["matches"],
                        "policy": policy_type,
                        "settings": settings,
                    }
                    for settings in _policy_settings(policy_type, body)
                )
    return rows


def parse_config_dump(dump: Any) -> dict[str, Any]:
    """Turn a /config_dump document into the four page sections."""
    dump = _as_dict(dump)
    warnings: list[str] = []
    backends = _backend_index(dump)
    routes = _routes(dump, backends)
    version = _as_dict(dump.get("version"))
    return sanitize({
        "version": str(version.get("version") or ""),
        "models": _parse_models(backends, routes),
        "tools": _parse_tools(backends, routes, warnings),
        "agents": _parse_agents(backends, routes),
        "traffic": _parse_traffic(routes),
        "warnings": warnings,
    })


def _parse_mcp_body(body: str, content_type: str) -> dict[str, Any]:
    """Read a JSON-RPC result from a plain JSON or a single-event SSE response."""
    if "text/event-stream" in content_type:
        for line in body.splitlines():
            if line.startswith("data:"):
                return _as_dict(_as_dict(json.loads(line[5:])).get("result"))
        return {}
    return _as_dict(_as_dict(json.loads(body)).get("result"))


def _mcp_list(client: httpx.Client, url: str, method: str) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    with client.stream("POST", url, json=payload, headers={"Accept": MCP_ACCEPT}) as response:
        response.raise_for_status()
        # Tool lists come from upstream MCP servers, so bound what's read from them.
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > MAX_MCP_RESPONSE_BYTES:
                raise ValueError("MCP response is too large")
        return _parse_mcp_body(body.decode("utf-8", "replace"), response.headers.get("content-type", ""))


def _discover_mcp(client: httpx.Client, url: str) -> dict[str, Any]:
    """List what the gateway's MCP endpoint serves now. This is live state, not config."""
    if not url:
        return {"discovered": None, "discoveryError": "MCP URL isn't configured"}
    try:
        tools = _mcp_list(client, url, "tools/list")
        resources = _mcp_list(client, url, "resources/list")
    except (httpx.HTTPError, ValueError) as exc:
        return {"discovered": None, "discoveryError": type(exc).__name__}
    return {
        "discovered": {
            "tools": [
                {"name": str(t.get("name") or ""), "description": str(t.get("description") or "")}
                for t in map(_as_dict, _as_list(tools.get("tools")))
            ],
            "resources": [
                {
                    "name": str(r.get("name") or ""),
                    "uri": str(r.get("uri") or ""),
                    "description": str(r.get("description") or ""),
                }
                for r in map(_as_dict, _as_list(resources.get("resources")))
            ],
            "partial": bool(tools.get("nextCursor") or resources.get("nextCursor")),
        },
        "discoveryError": None,
    }


def _cached_discovery(client: httpx.Client, url: str) -> dict[str, Any]:
    with _discovery_lock:
        cached = _discovery_cache.get("result")
    if cached is not None:
        return cached
    # Fetch without the lock, so a slow upstream can't block other requests.
    # Concurrent misses may each fetch, which is harmless for two read-only calls.
    result = _discover_mcp(client, url)
    with _discovery_lock:
        _discovery_cache["result"] = result
    return result


def _read_runtime(client: httpx.Client, config_url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    """Reload state is a nice-to-have: the page still works without it."""
    try:
        response = client.get(urljoin(config_url, "/api/runtime"), headers=headers)
        response.raise_for_status()
        reload_state = _as_dict(_as_dict(response.json()).get("configReload"))
    except (httpx.HTTPError, ValueError):
        return None
    # Leave out lastError: it's free text that can quote the config, and any logged-in
    # user can read this response. The detail is in the gateway's logs.
    return {"synchronized": bool(reload_state.get("synchronized"))}


def _host_resolves(url: str) -> bool:
    try:
        socket.getaddrinfo(urlsplit(url).hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    return True


def _summary(client: httpx.Client) -> dict[str, Any]:
    settings = get_settings()
    url = settings.AGENTGATEWAY_CONFIG_URL
    if not url:
        return {"state": "not_deployed", "message": "No agentgateway URL is configured."}

    headers = {"Authorization": f"Bearer {settings.AGENTGATEWAY_API_KEY}"}
    try:
        response = client.get(url, headers=headers)
    except httpx.ConnectError:
        if _host_resolves(url):
            return {"state": "unreachable", "message": "Couldn't connect to agentgateway."}
        return {"state": "not_deployed", "message": "The agentgateway service wasn't found."}
    except httpx.HTTPError as exc:
        return {"state": "unreachable", "message": f"Request failed: {type(exc).__name__}."}

    if response.status_code in (401, 403):
        return {"state": "unauthorized", "message": "agentgateway rejected the portal's API key."}
    if response.status_code != 200:
        return {"state": "unreachable", "message": f"agentgateway returned HTTP {response.status_code}."}
    try:
        dump = response.json()
    except ValueError:
        return {"state": "unreachable", "message": "agentgateway returned invalid JSON."}
    if not isinstance(dump, dict):
        return {"state": "unreachable", "message": "agentgateway returned an unexpected document."}

    return {
        "state": "ok",
        "message": "",
        **parse_config_dump(dump),
        "runtime": _read_runtime(client, url, headers),
        **sanitize(_cached_discovery(client, settings.AGENTGATEWAY_MCP_URL)),
    }


def get_ai_gateways_summary(client: httpx.Client | None = None) -> dict[str, Any]:
    if client is not None:
        return _summary(client)
    # trust_env=False: cluster addresses must never go through a proxy from the environment.
    with httpx.Client(timeout=4.0, trust_env=False, follow_redirects=False) as owned:
        return _summary(owned)
