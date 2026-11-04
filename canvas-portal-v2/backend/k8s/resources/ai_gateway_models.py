"""Add, change and remove LLM models in agentgateway's config.

The gateway reads one config file from a ConfigMap and reloads it when it changes, so
the gateway itself is the validator: each change is written, then checked against the
gateway's reload status, and put back if the gateway rejects it. A rejected file keeps
the old config running, but would stop the gateway from starting after a pod restart.

API keys go in a Secret that is mounted as files, and never leave this module.
"""

import base64
import hashlib
import json
import logging
import posixpath
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from config import get_settings
from k8s.client import get_base_api_client
from k8s.resources.ai_gateways import GatewayRequestRefused, _mask_prose, parse_config_dump

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.yaml"
FORM_PARAMS = (
    "model",
    "baseUrl",
    "awsRegion",
    "bedrockEndpointPreference",
    "vertexRegion",
    "vertexProject",
    "azureResourceName",
    "azureResourceType",
    "azureApiVersion",
    "azureProjectName",
    "tokenize",
)
FORM_FIELDS = {"name", "provider", "visibility", "passthrough", "params"}
NUDGE_ANNOTATION = "canvas-portal/config-nudge"
POLL_SECONDS = 0.5
# Nudged pods reload in about a second. Without the nudge, the kubelet takes up to about 90s.
NUDGED_DEADLINE_SECONDS = 20.0
UNNUDGED_DEADLINE_SECONDS = 100.0
MISSING_KEY_RETRY_SECONDS = 5.0
_GENERATED_KEY_NAME = re.compile(r"[0-9a-f]{16}")
# Settings that shape a request or who may make it, but not where it goes or how it's authenticated.
_FIELDS_THAT_KEEP_THE_UPSTREAM = {
    "id",
    "name",
    "visibility",
    "passthrough",
    "authorization",
    "defaults",
    "overrides",
    "transformation",
    "finalTransformation",
    "responseHeaders",
    "health",
    "promptCaching",
    "matches",
}
_change_lock = threading.Lock()


class _ConfigDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def models_are_configurable() -> bool:
    settings = get_settings()
    return bool(settings.AGENTGATEWAY_CONFIG_NAMESPACE and settings.AGENTGATEWAY_CONFIG_URL)


def _core_api() -> k8s.CoreV1Api:
    if not models_are_configurable():
        raise GatewayRequestRefused(503, "Changing models isn't configured for this portal.")
    return k8s.CoreV1Api(get_base_api_client())


def _refusal(exc: ApiException, action: str) -> GatewayRequestRefused:
    if exc.status == 403:
        return GatewayRequestRefused(502, f"The portal isn't allowed to {action}.")
    return GatewayRequestRefused(502, f"Couldn't {action}: HTTP {exc.status}.")


@dataclass
class _Config:
    document: dict[str, Any]
    models: list[Any]
    version: str
    text: str


def _read_config(core: k8s.CoreV1Api) -> _Config:
    settings = get_settings()
    try:
        config_map = core.read_namespaced_config_map(
            settings.AGENTGATEWAY_CONFIG_MAP, settings.AGENTGATEWAY_CONFIG_NAMESPACE
        )
    except ApiException as exc:
        raise _refusal(exc, "read the gateway config") from exc
    text = (config_map.data or {}).get(CONFIG_FILE, "")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GatewayRequestRefused(502, "The gateway config file can't be parsed.") from exc
    llm = _as_dict(_as_dict(document).get("llm"))
    if not llm:
        raise GatewayRequestRefused(409, "The gateway config has no llm section to add models to.")
    models = llm.setdefault("models", [])
    if not isinstance(models, list):
        raise GatewayRequestRefused(502, "The gateway config has an unexpected models list.")
    return _Config(document, models, config_map.metadata.resource_version, text)


def _write_config(core: k8s.CoreV1Api, text: str, version: str) -> str:
    settings = get_settings()
    body = k8s.V1ConfigMap(data={CONFIG_FILE: text}, metadata=k8s.V1ObjectMeta(resource_version=version))
    try:
        written = core.patch_namespaced_config_map(
            settings.AGENTGATEWAY_CONFIG_MAP, settings.AGENTGATEWAY_CONFIG_NAMESPACE, body
        )
    except ApiException as exc:
        if exc.status == 409:
            raise GatewayRequestRefused(409, "The gateway config changed at the same time. Try again.") from exc
        raise _refusal(exc, "update the gateway config") from exc
    return written.metadata.resource_version


def _render(document: dict[str, Any]) -> str:
    return yaml.dump(document, Dumper=_ConfigDumper, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _revision(entry: Any) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _view(entry: dict[str, Any]) -> dict[str, Any]:
    params = _as_dict(entry.get("params"))
    return {
        "name": entry.get("name"),
        "provider": entry.get("provider"),
        "visibility": entry.get("visibility"),
        "passthrough": entry.get("passthrough"),
        "params": {key: params[key] for key in FORM_PARAMS if key in params},
        "apiKeySet": params.get("apiKey") is not None,
        "advanced": {key: value for key, value in entry.items() if key not in FORM_FIELDS},
        "revision": _revision(entry),
    }


def list_model_configs(core: k8s.CoreV1Api | None = None) -> dict[str, Any]:
    if core is None and not models_are_configurable():
        return {"configurable": False, "models": []}
    config = _read_config(core or _core_api())
    return {"configurable": True, "models": [_view(entry) for entry in config.models if isinstance(entry, dict)]}


def _key_file_path(entry: dict[str, Any] | None) -> str | None:
    path = _as_dict(_as_dict(_as_dict(entry).get("params")).get("apiKey")).get("file")
    return path if isinstance(path, str) else None


def _unused_portal_key(entry: dict[str, Any] | None, remaining: list[Any]) -> str | None:
    """The Secret key this model no longer needs, if the portal created it and nothing else uses it."""
    path = _key_file_path(entry)
    if not path:
        return None
    folder, name = posixpath.split(path)
    if folder != get_settings().AGENTGATEWAY_KEYS_PATH or not _GENERATED_KEY_NAME.fullmatch(name):
        return None
    return None if any(_key_file_path(_as_dict(model)) == path for model in remaining) else name


def _update_keys(core: k8s.CoreV1Api, changes: dict[str, str | None]) -> None:
    settings = get_settings()
    try:
        core.patch_namespaced_secret(
            settings.AGENTGATEWAY_KEYS_SECRET, settings.AGENTGATEWAY_CONFIG_NAMESPACE, {"data": changes}
        )
    except ApiException as exc:
        raise _refusal(exc, "update the API key Secret") from exc


def _delete_key(core: k8s.CoreV1Api, name: str | None) -> None:
    """An unused key does no harm, so a failure here doesn't fail the change."""
    if not name:
        return
    try:
        _update_keys(core, {name: None})
    except GatewayRequestRefused as exc:
        logger.warning("Couldn't delete an unused model API key: %s", exc.message)


def _nudge_pods(core: k8s.CoreV1Api) -> bool:
    """Updating a pod makes the kubelet refresh its mounted files now, not within a minute or so."""
    settings = get_settings()
    namespace = settings.AGENTGATEWAY_CONFIG_NAMESPACE
    patch = {"metadata": {"annotations": {NUDGE_ANNOTATION: str(time.time_ns())}}}
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=settings.AGENTGATEWAY_POD_SELECTOR).items
        for pod in pods:
            core.patch_namespaced_pod(pod.metadata.name, namespace, patch)
    except ApiException as exc:
        logger.warning("Couldn't nudge the gateway pods: HTTP %s", exc.status)
        return False
    return bool(pods)


@dataclass
class _GatewayState:
    last_error: str | None
    dump: Any


def _gateway_client() -> httpx.Client:
    # trust_env=False: cluster addresses must never go through a proxy from the environment.
    return httpx.Client(timeout=4.0, trust_env=False, follow_redirects=False)


def _read_gateway(http: httpx.Client) -> _GatewayState:
    settings = get_settings()
    url = settings.AGENTGATEWAY_CONFIG_URL
    headers = {"Authorization": f"Bearer {settings.AGENTGATEWAY_API_KEY}"}
    runtime = http.get(urljoin(url, "/api/runtime"), headers=headers)
    runtime.raise_for_status()
    dump = http.get(url, headers=headers)
    dump.raise_for_status()
    last_error = _as_dict(_as_dict(runtime.json()).get("configReload")).get("lastError")
    return _GatewayState(str(last_error) if last_error else None, dump.json())


def _active_names(dump: Any) -> set[str]:
    return {model["name"] for model in parse_config_dump(dump)["models"]}


def _missing_key_file(error: str) -> bool:
    return f"read resource file {get_settings().AGENTGATEWAY_KEYS_PATH}/" in error


def _await_reload(
    core: k8s.CoreV1Api,
    http: httpx.Client,
    text: str,
    version: str,
    applied: Callable[[Any], bool],
    deadline_seconds: float,
) -> tuple[str | None, bool, str]:
    """Returns the gateway's error (if any), whether the change was seen, and the ConfigMap version."""
    deadline = time.monotonic() + deadline_seconds
    retries = 0
    last_retry = float("-inf")
    while True:
        try:
            state = _read_gateway(http)
        except (httpx.HTTPError, ValueError):
            state = None
        if state:
            if state.last_error and _missing_key_file(state.last_error):
                # The config reached the pod before the key file did, and the gateway doesn't
                # watch a file that was missing. Only a change to the config brings it back.
                # The error stays until the next reload, so space out the retries.
                if time.monotonic() - last_retry >= MISSING_KEY_RETRY_SECONDS:
                    last_retry = time.monotonic()
                    retries += 1
                    try:
                        version = _write_config(core, f"{text}\n# portal-reload {retries}\n", version)
                    except GatewayRequestRefused:
                        return state.last_error, False, version
                    _nudge_pods(core)
            elif state.last_error:
                return state.last_error, False, version
            elif applied(state.dump):
                return None, True, version
        if time.monotonic() >= deadline:
            return (state.last_error if state else None), False, version
        time.sleep(POLL_SECONDS)


def _await_recovery(http: httpx.Client, deadline_seconds: float) -> None:
    """The gateway reports its last error until it reloads, which would refuse the next change."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            if not _read_gateway(http).last_error:
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(POLL_SECONDS)


def _placeholders(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    """Each string with a $, with where it is, because moving one can send its value elsewhere."""
    if isinstance(value, str):
        return {(*path, value)} if "$" in value else set()
    if isinstance(value, dict):
        found: set[tuple[Any, ...]] = set()
        for key, item in value.items():
            found |= _placeholders(str(key), (*path, "key")) | _placeholders(item, (*path, str(key)))
        return found
    if isinstance(value, list):
        return {found for index, item in enumerate(value) for found in _placeholders(item, (*path, index))}
    return set()


def _file_references(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    """Each {file: ...} value, with where it is. The gateway reads these from its own pod."""
    if isinstance(value, dict):
        found: set[tuple[Any, ...]] = set()
        for key, item in value.items():
            if key == "file" and isinstance(item, str):
                found.add((*path, item))
            found |= _file_references(item, (*path, str(key)))
        return found
    if isinstance(value, list):
        return {found for index, item in enumerate(value) for found in _file_references(item, (*path, index))}
    return set()


def _refuse_new_file_references(entry: dict[str, Any], existing: dict[str, Any] | None, key_file: str | None) -> None:
    # A new one could send any file in the gateway's pod upstream as a credential, such as its
    # service account token or another model's key. Only the file for a key just entered is allowed.
    allowed = _file_references(existing)
    if key_file:
        allowed = allowed | {("params", "apiKey", f"{get_settings().AGENTGATEWAY_KEYS_PATH}/{key_file}")}
    if _file_references(entry) - allowed:
        raise GatewayRequestRefused(
            422, "File references can't be added here. Enter API keys in the API key field."
        )


def _refuse_new_placeholders(entry: dict[str, Any], existing: dict[str, Any] | None) -> None:
    # The gateway expands environment variables across its whole config file. A new one could
    # send the gateway's own secrets upstream, and an unknown one stops the file from loading.
    if _placeholders(entry) - _placeholders(existing):
        raise GatewayRequestRefused(
            422, "'$' can't be used here, because the gateway treats it as an environment variable."
        )


def _upstream_settings(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Everything that could change where a request goes, including settings added later."""
    settings = {key: value for key, value in _as_dict(entry).items() if key not in _FIELDS_THAT_KEEP_THE_UPSTREAM}
    settings["params"] = {
        key: value for key, value in _as_dict(settings.get("params")).items() if key not in ("model", "tokenize")
    }
    return settings


def _refuse_keeping_key_for_new_upstream(entry: dict[str, Any], existing: dict[str, Any] | None) -> None:
    # Otherwise, changing the upstream would send a secret the admin can't read to any host.
    if existing is None or _upstream_settings(entry) == _upstream_settings(existing):
        return
    old_key = _as_dict(existing.get("params")).get("apiKey")
    kept_key = old_key is not None and _as_dict(entry.get("params")).get("apiKey") == old_key
    kept_auth = existing.get("auth") is not None and entry.get("auth") == existing["auth"]
    kept_files = _file_references(entry) & _file_references(existing)
    if kept_key or kept_auth or kept_files or _placeholders(entry) & _placeholders(existing):
        raise GatewayRequestRefused(
            422, "This changes where requests go, so enter its API key and other credentials again or remove them."
        )


def _build_entry(
    request: dict[str, Any], existing: dict[str, Any] | None, key_file: str | None
) -> dict[str, Any]:
    clash = set(request["advanced"]) & FORM_FIELDS
    if clash:
        raise GatewayRequestRefused(422, f"Set {', '.join(sorted(clash))} in the form, not in the advanced settings.")
    provider = request["provider"]
    if not provider:
        raise GatewayRequestRefused(422, "A provider is required.")
    if isinstance(provider, dict) and (
        set(provider) not in ({"custom"}, {"reference"})
        or not (isinstance(provider.get("custom"), dict) or isinstance(provider.get("reference"), str))
    ):
        raise GatewayRequestRefused(422, "The provider must be a name, a custom provider or a reference.")
    base_url = request["params"].get("baseUrl")
    if base_url and urlsplit(base_url).scheme not in ("http", "https"):
        raise GatewayRequestRefused(422, "The base URL must start with http:// or https://.")

    old_params = _as_dict(_as_dict(existing).get("params"))
    params = {key: value for key, value in old_params.items() if key not in FORM_PARAMS}
    params.update({key: value for key, value in request["params"].items() if value not in (None, "")})
    if key_file:
        params["apiKey"] = {"file": f"{get_settings().AGENTGATEWAY_KEYS_PATH}/{key_file}"}
    elif request["removeApiKey"]:
        params.pop("apiKey", None)

    entry: dict[str, Any] = {"name": request["name"], "provider": provider}
    for field in ("visibility", "passthrough"):
        if request.get(field):
            entry[field] = request[field]
    if params:
        entry["params"] = params
    entry.update(request["advanced"])
    _refuse_new_placeholders(entry, existing)
    _refuse_new_file_references(entry, existing, key_file)
    _refuse_keeping_key_for_new_upstream(entry, existing)
    return entry


def _change_config(
    core: k8s.CoreV1Api,
    http: httpx.Client,
    config: _Config,
    applied: Callable[[Any], bool],
    new_key: tuple[str, str] | None,
    old_key_file: str | None,
) -> dict[str, Any]:
    """Writes config.document, checks that the gateway took it, and puts it back if not."""
    new_key_file = new_key[0] if new_key else None

    if new_key:
        encoded = base64.b64encode(new_key[1].encode()).decode()
        _update_keys(core, {new_key[0]: encoded})
        _nudge_pods(core)
    text = _render(config.document)
    try:
        version = _write_config(core, text, config.version)
    except GatewayRequestRefused:
        _delete_key(core, new_key_file)
        raise
    nudged = _nudge_pods(core)
    deadline = NUDGED_DEADLINE_SECONDS if nudged else UNNUDGED_DEADLINE_SECONDS
    error, seen, version = _await_reload(core, http, text, version, applied, deadline)

    if error:
        try:
            _write_config(core, config.text, version)
        except GatewayRequestRefused as exc:
            logger.error("Couldn't undo a rejected model change: %s", exc.message)
            raise GatewayRequestRefused(
                502, "The gateway rejected this change, and it couldn't be undone. Check the gateway config."
            ) from exc
        nudged = _nudge_pods(core)
        _await_recovery(http, NUDGED_DEADLINE_SECONDS if nudged else UNNUDGED_DEADLINE_SECONDS)
        _delete_key(core, new_key_file)
        raise GatewayRequestRefused(
            422, f"The gateway rejected this change, so it was undone: {_mask_prose(error)}"
        )
    if seen:
        _delete_key(core, old_key_file)
        return {"applied": True, "message": "The gateway is using the new configuration."}
    # The gateway may still be about to load this, so a replaced key stays in the Secret.
    return {
        "applied": False,
        "message": "Saved. The gateway hasn't reported an error, but this change can't be confirmed from its status.",
    }


def _ready_to_change(http: httpx.Client) -> _GatewayState:
    try:
        state = _read_gateway(http)
    except (httpx.HTTPError, ValueError) as exc:
        raise GatewayRequestRefused(502, "Couldn't read the gateway's status.") from exc
    if state.last_error:
        # A later error couldn't be told apart from this one, so the check after writing wouldn't work.
        raise GatewayRequestRefused(409, "The gateway failed to load its current config. Fix that first.")
    return state


def _find(config: _Config, name: str) -> dict[str, Any] | None:
    return next((m for m in config.models if isinstance(m, dict) and m.get("name") == name), None)


def _with_change_lock(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    if not _change_lock.acquire(blocking=False):
        raise GatewayRequestRefused(409, "Another model change is still being applied. Try again shortly.")
    try:
        return run()
    finally:
        _change_lock.release()


def _save(
    request: dict[str, Any],
    original_name: str | None,
    revision: str | None,
    core: k8s.CoreV1Api,
    http: httpx.Client,
) -> dict[str, Any]:
    config = _read_config(core)
    existing = _find(config, original_name) if original_name else None
    if original_name and existing is None:
        raise GatewayRequestRefused(404, "This model isn't in the gateway config any more.")
    if existing is not None and _revision(existing) != revision:
        raise GatewayRequestRefused(409, "This model was changed after you opened it. Reopen it to see the changes.")
    other = _find(config, request["name"])
    if other is not None and other is not existing:
        raise GatewayRequestRefused(409, "A model with this name already exists.")

    new_key = (secrets.token_hex(8), request["apiKey"]) if request["apiKey"] else None
    entry = _build_entry(request, existing, new_key[0] if new_key else None)
    if entry == existing:
        return {"applied": True, "message": "Nothing changed."}
    before = _ready_to_change(http)

    if existing is None:
        config.models.append(entry)
    else:
        config.models[config.models.index(existing)] = entry

    if existing is not None and request["name"] == original_name:
        applied = lambda dump: dump != before.dump
    else:
        applied = lambda dump: request["name"] in _active_names(dump)
    # A replaced or removed key is deleted only once the gateway has taken the change.
    old_key_file = _unused_portal_key(existing, config.models) if new_key or request["removeApiKey"] else None
    return _change_config(core, http, config, applied, new_key, old_key_file)


def save_model(
    request: dict[str, Any],
    original_name: str | None = None,
    revision: str | None = None,
    core: k8s.CoreV1Api | None = None,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    core = core or _core_api()
    if http is not None:
        return _with_change_lock(lambda: _save(request, original_name, revision, core, http))
    with _gateway_client() as owned:
        return _with_change_lock(lambda: _save(request, original_name, revision, core, owned))


def _remove(name: str, core: k8s.CoreV1Api, http: httpx.Client) -> dict[str, Any]:
    config = _read_config(core)
    existing = _find(config, name)
    if existing is None:
        raise GatewayRequestRefused(404, "This model isn't in the gateway config any more.")
    _ready_to_change(http)
    config.models.remove(existing)
    return _change_config(
        core, http, config, lambda dump: name not in _active_names(dump), None, _unused_portal_key(existing, config.models)
    )


def remove_model(name: str, core: k8s.CoreV1Api | None = None, http: httpx.Client | None = None) -> dict[str, Any]:
    core = core or _core_api()
    if http is not None:
        return _with_change_lock(lambda: _remove(name, core, http))
    with _gateway_client() as owned:
        return _with_change_lock(lambda: _remove(name, core, owned))
