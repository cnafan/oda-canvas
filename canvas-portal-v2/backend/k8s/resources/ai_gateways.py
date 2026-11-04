"""Read the active agentgateway configuration for the AI Gateways page.

The source is agentgateway's /config_dump, a debug API with no stability
guarantee, so parsing is defensive. Only whitelisted fields leave this module.
"""

import json
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx
from cachetools import TTLCache

from config import get_settings

MCP_ACCEPT = "application/json, text/event-stream"
MAX_MCP_RESPONSE_BYTES = 1024 * 1024
MCP_DISCOVERY_DEADLINE_SECONDS = 8.0
MAX_MCP_TEXT_LENGTH = 2048

LLM_TEST_PROMPT = "PING"
# Models that reason first can use most of this before replying: 64 nearly cut off a PING.
LLM_TEST_MAX_TOKENS = 256
LLM_TEST_TIMEOUT = httpx.Timeout(10.0, read=60.0)
LLM_TEST_DEADLINE_SECONDS = 120.0
MAX_LLM_TEST_RESPONSE_BYTES = 1024 * 1024

# Case-sensitive `[Tt]oken$`, so rate-limit fields such as maxTokens stay visible.
_SECRET_KEY = re.compile(
    r"[Ss]ecret|[Pp]assword|[Aa]pi[-_]?[Kk]ey|[Aa]uthorization|[Cc]ookie|[Cc]redential|[Tt]oken$|^key$"
)
# user:password@host at the start of a word or after a slash. It needs a colon before
# the last @, so plain email addresses stay readable.
_USERINFO_WITH_PASSWORD = r"(?<![^\s/])[^/\s:]+:[^/\s]+(?=@)"
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+"  # Authorization values
    r"|\bsk-[A-Za-z0-9_-]{8,}"  # Provider API keys
    r"|(?<=://)[^/\s]*(?=@)"  # URL userinfo, up to the last @ in the authority
    r"|^[^/\s]*(?=@)"  # Userinfo on a host with no scheme
    rf"|{_USERINFO_WITH_PASSWORD}"
)
# For MCP descriptions only: an auth scheme counts when followed by a credential-length
# token, so that prose such as "Basic calculator" stays readable.
_SECRET_IN_PROSE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=._~-]{16,}"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|(?<=://)[^/\s]*(?=@)"
    rf"|{_USERINFO_WITH_PASSWORD}"
)
_PARAM_START = re.compile(r"[?&;#][\w-]*=")
_PARAM_VALUE_END = re.compile(r"[&;#\s]")
_SECRET_PARAM_NAME = re.compile(r"(?i)key|token|secret|password|credential|auth|sig")

# Policies shown elsewhere on the page, or with nothing to show.
_NON_TRAFFIC_POLICIES = {"a2a", "budget"}
_POLICY_FIELDS = {
    "timeout": ("requestTimeout", "backendRequestTimeout"),
    "retry": ("attempts", "backoff", "codes"),
    "localRateLimit": ("type", "maxTokens", "tokensPerFill", "fillInterval"),
}

_discovery_cache: TTLCache = TTLCache(maxsize=1, ttl=30)
_discovery_lock = threading.Lock()


class _Backend(NamedTuple):
    kind: str
    body: dict[str, Any]
    policies: list[Any]


_MISSING_BACKEND = _Backend("", {}, [])


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
        return _mask_params(_SECRET_VALUE.sub("<redacted>", value))
    return value


def _mask_params(text: str) -> str:
    """Mask secret-like parameter values, including parameters nested in another's value.

    A secret value runs to the next delimiter, even past a `?`. This is one scan,
    so its time stays linear on hostile input.
    """
    parts = []
    position = 0
    for match in _PARAM_START.finditer(text):
        if match.start() < position or not _SECRET_PARAM_NAME.search(match.group()):
            continue
        end = _PARAM_VALUE_END.search(text, match.end())
        parts += [text[position:match.end()], "<redacted>"]
        position = end.start() if end else len(text)
    parts.append(text[position:])
    return "".join(parts)


def _mcp_text(value: Any) -> str:
    """Upstream servers control this text, so limit its length. Mask first, so a cut can't hide a secret."""
    return sanitize(str(value or ""))[:MAX_MCP_TEXT_LENGTH]


def _mcp_description(value: Any) -> str:
    masked = _mask_params(_SECRET_IN_PROSE.sub("<redacted>", str(value or "")))
    return masked[:MAX_MCP_TEXT_LENGTH]


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


def _backend_index(dump: dict[str, Any]) -> dict[str, _Backend]:
    index = {}
    for entry in _as_list(dump.get("backends")):
        entry = _as_dict(entry)
        for kind, body in _as_dict(entry.get("backend")).items():
            body = _as_dict(body)
            name = body.get("name")
            if isinstance(name, str):
                index[name] = _Backend(kind, body, _as_list(entry.get("inlinePolicies")))
    return index


def _backend_ref(ref: Any) -> str:
    return ref.lstrip("/") if isinstance(ref, str) else ""


def _routes(dump: dict[str, Any], backends: dict[str, _Backend]) -> list[dict[str, Any]]:
    """Flatten routes, leaving out the gateway's own read path (internal backends only)."""
    routes = []
    for bind in _as_list(dump.get("binds")):
        for listener in _as_dict(_as_dict(bind).get("listeners")).values():
            for route in _as_dict(_as_dict(listener).get("routes")).values():
                route = _as_dict(route)
                refs = [_as_dict(b) for b in _as_list(route.get("backends"))]
                names = [_backend_ref(b.get("backend")) for b in refs]
                kinds = [backends.get(n, _MISSING_BACKEND).kind for n in names]
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


def _parse_models(backends: dict[str, _Backend], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    routers = [name for name, backend in backends.items() if backend.kind == "llmRouter"]
    models = []
    for name, backend in backends.items():
        if backend.kind != "ai":
            continue
        match = _routes_to(routes, {name, *routers})
        for group in _as_list(_as_dict(backend.body.get("target")).get("providers")):
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
    backends: dict[str, _Backend], routes: list[dict[str, Any]], warnings: list[str]
) -> list[dict[str, Any]]:
    tools = []
    for name, backend in backends.items():
        if backend.kind != "mcp":
            continue
        target_config = _as_dict(backend.body.get("target"))
        match = _routes_to(routes, {name})
        for target in _as_list(target_config.get("targets")):
            target = _as_dict(target)
            transport, spec = next(
                ((k, v) for k, v in target.items() if isinstance(v, dict)), ("", {})
            )
            ref = _backend_ref(_as_dict(spec.get("backend")).get("backend"))
            resolved = backends.get(ref, _MISSING_BACKEND)
            host = resolved.body.get("target") if resolved.kind == "host" else None
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


def _parse_agents(backends: dict[str, _Backend], routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents = []
    for route in routes:
        for ref, name in zip(route["refs"], route["backendNames"]):
            resolved = backends.get(name, _MISSING_BACKEND)
            if not (
                _has_a2a(_as_list(ref.get("inlinePolicies")))
                or _has_a2a(route["policies"])
                or _has_a2a(resolved.policies)
            ):
                continue
            target = resolved.body.get("target")
            agents.append({
                "name": route["name"],
                "matches": route["matches"],
                "backend": target if isinstance(target, str) else name,
            })
    return agents


def _policy_settings(policy_type: str, body: Any) -> list[dict[str, Any]]:
    if policy_type == "urlRewrite":
        path = _as_dict(_as_dict(body).get("path"))
        return [{field: path[field] for field in ("prefix", "full") if field in path}]
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
            data = line[5:].strip() if line.startswith("data:") else ""
            if data:
                return _as_dict(_as_dict(json.loads(data)).get("result"))
        return {}
    return _as_dict(_as_dict(json.loads(body)).get("result"))


def _mcp_list(client: httpx.Client, url: str, method: str, deadline: float) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    # Tool lists come from upstream MCP servers, so limit what's read from them. Count
    # raw bytes and refuse compression, so the limit can't be passed by decompressing.
    headers = {"Accept": MCP_ACCEPT, "Accept-Encoding": "identity"}
    with client.stream("POST", url, json=payload, headers=headers) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("MCP response is compressed")
        body = bytearray()
        for chunk in response.iter_raw():
            body += chunk
            if len(body) > MAX_MCP_RESPONSE_BYTES:
                raise ValueError("MCP response is too large")
            if time.monotonic() > deadline:
                raise ValueError("MCP response took too long")
        return _parse_mcp_body(body.decode("utf-8", "replace"), response.headers.get("content-type", ""))


def _discover_mcp(client: httpx.Client, url: str) -> dict[str, Any]:
    """List what the gateway's MCP endpoint serves now. This is live state, not config."""
    if not url:
        return {"discovered": None, "discoveryError": "MCP URL isn't configured"}
    try:
        deadline = time.monotonic() + MCP_DISCOVERY_DEADLINE_SECONDS
        tools = _mcp_list(client, url, "tools/list", deadline)
        resources = _mcp_list(client, url, "resources/list", deadline)
    except (httpx.HTTPError, ValueError) as exc:
        return {"discovered": None, "discoveryError": type(exc).__name__}
    return {
        "discovered": {
            "tools": [
                {"name": _mcp_text(t.get("name")), "description": _mcp_description(t.get("description"))}
                for t in map(_as_dict, _as_list(tools.get("tools")))
            ],
            "resources": [
                {
                    "name": _mcp_text(r.get("name")),
                    "uri": _mcp_text(r.get("uri")),
                    "description": _mcp_description(r.get("description")),
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
        **_cached_discovery(client, settings.AGENTGATEWAY_MCP_URL),
    }


def get_ai_gateways_summary(client: httpx.Client | None = None) -> dict[str, Any]:
    if client is not None:
        return _summary(client)
    # trust_env=False: cluster addresses must never go through a proxy from the environment.
    with httpx.Client(timeout=4.0, trust_env=False, follow_redirects=False) as owned:
        return _summary(owned)


class ModelTestRefused(Exception):
    """The test can't start. The message is safe to show to the user."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _llm_client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    # trust_env=False: cluster addresses must never go through a proxy from the environment.
    return httpx.AsyncClient(
        transport=transport, timeout=LLM_TEST_TIMEOUT, trust_env=False, follow_redirects=False
    )


async def ensure_model_is_active(model: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Only models the gateway has active can be tested, so this can't reach anything else."""
    settings = get_settings()
    if not settings.AGENTGATEWAY_CONFIG_URL or not settings.AGENTGATEWAY_LLM_URL:
        raise ModelTestRefused(503, "Model testing isn't configured.")
    try:
        async with _llm_client(transport) as client:
            response = await client.get(
                settings.AGENTGATEWAY_CONFIG_URL,
                headers={"Authorization": f"Bearer {settings.AGENTGATEWAY_API_KEY}"},
            )
        response.raise_for_status()
        models = parse_config_dump(response.json())["models"]
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelTestRefused(502, "Couldn't read the agentgateway configuration.") from exc
    if not any(m["name"] == model and m["status"] == "active" for m in models):
        raise ModelTestRefused(404, "This model isn't active on the gateway.")


def _event(**fields: Any) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def _llm_line_events(line: bytes) -> Iterator[dict[str, Any]]:
    """Read one server-sent event line, keeping only the text and how it finished."""
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return
    data = text[5:].strip()
    if data == "[DONE]":
        yield {"type": "end"}
        return
    try:
        chunk = _as_dict(json.loads(data))
    except ValueError:
        yield {"type": "error", "message": "The model sent an invalid stream."}
        return
    if "error" in chunk:
        # Fixed text: the upstream's error can quote its address or request details.
        yield {"type": "error", "message": "The model reported an error."}
        return
    for choice in map(_as_dict, _as_list(chunk.get("choices"))):
        delta = _as_dict(choice.get("delta"))
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if isinstance(reasoning, str):
            yield {"type": "reasoning", "text": reasoning}
        if isinstance(delta.get("content"), str) and delta["content"]:
            yield {"type": "content", "text": delta["content"]}
        if isinstance(choice.get("finish_reason"), str):
            yield {"type": "finish", "reason": choice["finish_reason"]}


def _llm_status_message(status_code: int) -> str:
    if status_code == 429:
        return "The gateway's rate limit was reached. Try again in a minute."
    return f"agentgateway returned HTTP {status_code}."


async def stream_model_test(
    model: str, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[bytes]:
    """Send PING to a model through the gateway, and yield its reply as NDJSON events.

    Events are `reasoning` and `content` with a `text`, then one `done` or `error`.
    Closing this generator closes the upstream request, which stops the generation.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": LLM_TEST_PROMPT}],
        "stream": True,
        "max_tokens": LLM_TEST_MAX_TOKENS,
    }
    # Count raw bytes and refuse compression, so the limit can't be passed by decompressing.
    headers = {"Accept": "text/event-stream", "Accept-Encoding": "identity"}
    deadline = time.monotonic() + LLM_TEST_DEADLINE_SECONDS
    try:
        async with _llm_client(transport) as client, client.stream(
            "POST", get_settings().AGENTGATEWAY_LLM_URL, json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                yield _event(type="error", message=_llm_status_message(response.status_code))
                return
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                yield _event(type="error", message="The model sent a compressed response.")
                return
            finish_reason = None
            pending = b""
            received = 0
            async for chunk in response.aiter_raw():
                received += len(chunk)
                if received > MAX_LLM_TEST_RESPONSE_BYTES or time.monotonic() > deadline:
                    yield _event(type="error", message="The reply was too long, so the test stopped.")
                    return
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    for event in _llm_line_events(line):
                        if event["type"] == "finish":
                            finish_reason = event["reason"]
                        elif event["type"] == "end":
                            yield _event(type="done", finishReason=finish_reason)
                            return
                        else:
                            yield _event(**event)
                            if event["type"] == "error":
                                return
    except httpx.HTTPError as exc:
        yield _event(type="error", message=f"Request failed: {type(exc).__name__}.")
        return
    if finish_reason:
        yield _event(type="done", finishReason=finish_reason)
    else:
        yield _event(type="error", message="The stream ended before the model finished.")
