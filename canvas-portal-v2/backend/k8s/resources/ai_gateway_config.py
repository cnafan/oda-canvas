"""Change agentgateway's config file safely, for the AI Gateways page.

The gateway reads one config file from a ConfigMap and reloads it when it changes, so
the gateway itself is the validator: each change is written, then checked against the
gateway's reload status, and put back if the gateway rejects it. A rejected file keeps
the old config running, but would stop the gateway from starting after a pod restart.

Secrets go in a Secret that is mounted as files, and never leave this module.
"""

import base64
import hashlib
import ipaddress
import json
import logging
import posixpath
import re
import socket
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
from k8s.resources.ai_gateways import GatewayRequestRefused, _mask_prose

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.yaml"
NUDGE_ANNOTATION = "canvas-portal/config-nudge"
POLL_SECONDS = 0.5
# Nudged pods reload in about a second. Without the nudge, the kubelet takes up to about 90s.
NUDGED_DEADLINE_SECONDS = 20.0
UNNUDGED_DEADLINE_SECONDS = 100.0
MISSING_KEY_RETRY_SECONDS = 5.0
LISTING_RETRY_SECONDS = 1.0
_GENERATED_KEY_NAME = re.compile(r"[0-9a-f]{16}")
_ADDRESS_FIELDS = {"host", "hostname", "proxy", "url", "uri", "endpoint", "tokenEndpoint", "issuer"}
_BACKEND_AUTH_FIELDS = {"auth", "backendAuth"}
_HOST_AND_PORT = re.compile(r"[^\s/:]+:\d+(/\S*)?")
_TLS_FIELDS = {"tls", "backendTLS"}
_TLS_PATH_FIELDS = {"cert", "key", "root"}
_change_lock = threading.Lock()


class _ConfigDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def config_is_changeable() -> bool:
    settings = get_settings()
    return bool(settings.AGENTGATEWAY_CONFIG_NAMESPACE and settings.AGENTGATEWAY_CONFIG_URL)


def core_api() -> k8s.CoreV1Api:
    if not config_is_changeable():
        raise GatewayRequestRefused(503, "Changing the gateway config isn't configured for this portal.")
    return k8s.CoreV1Api(get_base_api_client())


def _refusal(exc: ApiException, action: str) -> GatewayRequestRefused:
    if exc.status == 403:
        return GatewayRequestRefused(502, f"The portal isn't allowed to {action}.")
    return GatewayRequestRefused(502, f"Couldn't {action}: HTTP {exc.status}.")


@dataclass
class GatewayConfig:
    document: dict[str, Any]
    version: str
    text: str

    def entries(self, section: str, field: str) -> list[Any]:
        """The list to change, for example llm.models. Changes to it are part of the document."""
        block = as_dict(self.document.get(section))
        if not block:
            raise GatewayRequestRefused(409, f"The gateway config has no {section} section to change.")
        entries = block.setdefault(field, [])
        if not isinstance(entries, list):
            raise GatewayRequestRefused(502, f"The gateway config has an unexpected {section}.{field} list.")
        return entries


def read_config(core: k8s.CoreV1Api) -> GatewayConfig:
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
    return GatewayConfig(as_dict(document), config_map.metadata.resource_version, text)


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


def revision(entry: Any) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:16]


def unused_portal_key(path: str | None, document: dict[str, Any]) -> str | None:
    """The Secret key behind this path, if the portal created it and nothing in the document uses it."""
    if not path:
        return None
    folder, name = posixpath.split(path)
    if folder != get_settings().AGENTGATEWAY_KEYS_PATH or not _GENERATED_KEY_NAME.fullmatch(name):
        return None
    return None if any(reference[-1] == path for reference in file_references(document)) else name


def key_file_path(key_file: str) -> str:
    return f"{get_settings().AGENTGATEWAY_KEYS_PATH}/{key_file}"


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
class GatewayState:
    last_error: str | None
    dump: Any


def _read_gateway(http: httpx.Client) -> GatewayState:
    settings = get_settings()
    url = settings.AGENTGATEWAY_CONFIG_URL
    headers = {"Authorization": f"Bearer {settings.AGENTGATEWAY_API_KEY}"}
    runtime = http.get(urljoin(url, "/api/runtime"), headers=headers)
    runtime.raise_for_status()
    dump = http.get(url, headers=headers)
    dump.raise_for_status()
    last_error = as_dict(as_dict(runtime.json()).get("configReload")).get("lastError")
    return GatewayState(str(last_error) if last_error else None, dump.json())


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


def _await_recovery(http: httpx.Client, before: GatewayState, deadline_seconds: float) -> None:
    """Waits until the gateway has loaded the restored file, so the next change isn't refused."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            state = _read_gateway(http)
            if not state.last_error and state.dump == before.dump:
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(POLL_SECONDS)


def placeholders(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    """Each string with a $, with where it is, because moving one can send its value elsewhere."""
    if isinstance(value, str):
        return {(*path, value)} if "$" in value else set()
    if isinstance(value, dict):
        found: set[tuple[Any, ...]] = set()
        for key, item in value.items():
            found |= placeholders(str(key), (*path, "key")) | placeholders(item, (*path, str(key)))
        return found
    if isinstance(value, list):
        return {found for index, item in enumerate(value) for found in placeholders(item, (*path, index))}
    return set()


def file_references(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    """Each file path in these settings, with where it is. The gateway reads them from its own pod.

    In agentgateway v1.7.0's model and MCP target schemas, these are {file: ...} values and the TLS paths.
    """
    if isinstance(value, dict):
        in_tls = bool(path) and path[-1] in _TLS_FIELDS
        found: set[tuple[Any, ...]] = set()
        for key, item in value.items():
            if isinstance(item, str) and (key == "file" or (in_tls and key in _TLS_PATH_FIELDS)):
                found.add((*path, str(key), item))
            found |= file_references(item, (*path, str(key)))
        return found
    if isinstance(value, list):
        return {found for index, item in enumerate(value) for found in file_references(item, (*path, index))}
    return set()


def refuse_new_secrets(
    entry: dict[str, Any], existing: dict[str, Any] | None, allowed_new_file: tuple[Any, ...] | None
) -> None:
    # The gateway expands environment variables across its whole config file. A new one could
    # send the gateway's own secrets upstream, and an unknown one stops the file from loading.
    if placeholders(entry) - placeholders(existing):
        raise GatewayRequestRefused(
            422, "'$' can't be used here, because the gateway treats it as an environment variable."
        )
    # A new file could send anything in the gateway's pod upstream as a credential, such as its
    # service account token or another entry's key. Only the file for a key just entered is allowed.
    allowed = file_references(existing) | ({allowed_new_file} if allowed_new_file else set())
    if file_references(entry) - allowed:
        raise GatewayRequestRefused(
            422, "File references can't be added here. Enter keys and tokens in their own field."
        )
    if _implicit_credentials(entry) - _implicit_credentials(existing):
        raise GatewayRequestRefused(
            422, "Only a key can be added as backend auth here. Other kinds can send the gateway's own identity upstream."
        )
    # These can sit anywhere in the settings, such as a tunnel proxy, a webhook or a token endpoint.
    new_addresses = _address_like_values(entry) - _address_like_values(existing)
    if any(_points_at_gateway(address) for address in new_addresses):
        raise GatewayRequestRefused(422, _GATEWAY_ADDRESS_MESSAGE)


def refuse_keeping_secrets_for_new_upstream(entry: dict[str, Any], existing: dict[str, Any] | None) -> None:
    # Otherwise, changing the upstream would send a secret the admin can't read to any host.
    if file_references(entry) & file_references(existing) or placeholders(entry) & placeholders(existing):
        raise GatewayRequestRefused(
            422, "This changes where requests go, so enter its credentials again or remove them."
        )


def _is_gateway_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    address = mapped or address
    return address.is_loopback or address.is_link_local or address.is_unspecified


def _points_at_gateway(host: str) -> bool:
    target = host if "://" in host else f"//{host}"
    try:
        hostname = (urlsplit(target).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        # Forms like 2130706433 and 0x7f.1 also reach loopback.
        if re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*", hostname):
            return True
        if not hostname:
            return False
        try:
            infos = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, UnicodeError):
            return False  # The gateway's own check reports a name that doesn't resolve.
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    return any(_is_gateway_local(address) for address in addresses)


_GATEWAY_ADDRESS_MESSAGE = "This address points at the gateway itself or at a link-local address."


def refuse_gateway_local_host(host: str) -> None:
    """The gateway's admin port has no auth and is only on loopback, and its POST /quitquitquit stops it."""
    target = host if "://" in host else f"//{host}"
    try:
        has_host = bool(urlsplit(target).hostname)
    except ValueError:
        has_host = False
    if not has_host:
        raise GatewayRequestRefused(422, "This address has no host name.")
    if _points_at_gateway(host):
        raise GatewayRequestRefused(422, _GATEWAY_ADDRESS_MESSAGE)


def _address_like_values(value: Any, key: str | None = None) -> set[str]:
    if isinstance(value, str):
        looks_like_one = key in _ADDRESS_FIELDS or "://" in value or _HOST_AND_PORT.fullmatch(value)
        return {value} if looks_like_one else set()
    if isinstance(value, dict):
        return {found for k, item in value.items() for found in _address_like_values(item, str(k))}
    if isinstance(value, list):
        return {found for item in value for found in _address_like_values(item, key)}
    return set()


def _implicit_credentials(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    """Backend auth other than a key, which sends the pod's cloud identity or the caller's token."""
    found: set[tuple[Any, ...]] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _BACKEND_AUTH_FIELDS and not (isinstance(item, dict) and set(item) == {"key"}):
                found.add((*path, str(key), json.dumps(item, sort_keys=True, default=str)))
            found |= _implicit_credentials(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found |= _implicit_credentials(item, (*path, index))
    return found


def change_config(
    core: k8s.CoreV1Api,
    http: httpx.Client,
    config: GatewayConfig,
    before: GatewayState,
    applied: Callable[[Any], bool],
    new_key: tuple[str, str] | None,
    old_key_file: str | None,
    verify: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Writes config.document, checks that the gateway took it, and puts it back if not.

    verify returns a problem to undo the change for, for what the reload status can't show.
    If it passes, a replaced key can go even when the dump can't show the change.
    """
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

    message = f"The gateway rejected this change, so it was undone: {_mask_prose(error)}" if error else None
    if not error and verify and (problem := verify()):
        message = f"This change was undone: {_mask_prose(problem)}"
    if message:
        try:
            _write_config(core, config.text, version)
        except GatewayRequestRefused as exc:
            logger.error("Couldn't undo a gateway config change: %s", exc.message)
            raise GatewayRequestRefused(
                502, "This change failed, and it couldn't be undone. Check the gateway config."
            ) from exc
        nudged = _nudge_pods(core)
        _await_recovery(http, before, NUDGED_DEADLINE_SECONDS if nudged else UNNUDGED_DEADLINE_SECONDS)
        _delete_key(core, new_key_file)
        raise GatewayRequestRefused(422, message)
    if seen or verify:
        _delete_key(core, old_key_file)
    if seen:
        return {"applied": True, "message": "The gateway is using the new configuration."}
    # The gateway may still be about to load this, so a replaced key stays in the Secret.
    return {
        "applied": False,
        "message": "Saved. The gateway hasn't reported an error, but this change can't be confirmed from its status.",
    }


def ready_to_change(http: httpx.Client) -> GatewayState:
    try:
        state = _read_gateway(http)
    except (httpx.HTTPError, ValueError) as exc:
        raise GatewayRequestRefused(502, "Couldn't read the gateway's status.") from exc
    if state.last_error:
        # A later error couldn't be told apart from this one, so the check after writing wouldn't work.
        raise GatewayRequestRefused(409, "The gateway failed to load its current config. Fix that first.")
    return state


def find_entry(entries: list[Any], name: str) -> dict[str, Any] | None:
    return next((entry for entry in entries if isinstance(entry, dict) and entry.get("name") == name), None)


def run_change(
    run: Callable[[k8s.CoreV1Api, httpx.Client], dict[str, Any]],
    core: k8s.CoreV1Api | None,
    http: httpx.Client | None,
) -> dict[str, Any]:
    core = core or core_api()
    if not _change_lock.acquire(blocking=False):
        raise GatewayRequestRefused(409, "Another gateway change is still being applied. Try again shortly.")
    try:
        if http is not None:
            return run(core, http)
        # trust_env=False: cluster addresses must never go through a proxy from the environment.
        with httpx.Client(timeout=4.0, trust_env=False, follow_redirects=False) as owned:
            return run(core, owned)
    finally:
        _change_lock.release()
