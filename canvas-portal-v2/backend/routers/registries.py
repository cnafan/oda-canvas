"""Read-only proxy to OpenAN Registry Centers, and management of external ones.

The browser cannot call a registry directly (CSP ``connect-src 'self'``, no CORS
upstream, and credentials must stay server-side). Registry calls are only ever
GETs, even with an admin credential. The in-cluster registry comes from the chart;
external ones are added at runtime by portal admins and treated as untrusted.
"""
import base64
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from auth.middleware import get_current_user, is_admin, require_admin
from auth.models import AuthUser
from config import get_settings
from k8s import registry_store
from utils import outbound
from utils.exceptions import CanvasPortalError
from utils.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registries", tags=["registries"])

API_BASE = "/rest/v1/registry-center"
TIMEOUT_SECONDS = 5.0
REVIEW_HISTORY_LIMIT = 100
DOT_SEGMENTS = {".", ".."}

# Where an OpenAN Portal's gateway exposes its Registry Center.
PORTAL_REGISTRY_PATH = "/api/registry-center"

BUILTIN_ID = "in-cluster"
BUILTIN_NAME = "In-cluster"

FAN_OUT_DEADLINE_SECONDS = 6.0
CACHE_TTL_SECONDS = 30.0
MAX_OAUTH_TOKEN_SECONDS = 3600
MAX_PASSTHROUGH_DEPTH = 16
# A Registry Center bans a client for 300s after 5 failed authentications, so
# after a rejection neither page loads, Refresh nor retests may call it again soon.
LOCKOUT_TTL_SECONDS = 300.0
LOCKOUT_KINDS = {"bad_credentials", "rate_limited", "token_endpoint_failed"}

FAILURE_MESSAGES = {
    "invalid_url": "The URL is not allowed.",
    "blocked": "The address resolves to a loopback, link-local or otherwise blocked range.",
    "dns_error": "The host name could not be resolved.",
    "unreachable": "The registry could not be reached.",
    "timeout": "The registry did not respond in time.",
    "tls": "The registry's TLS certificate could not be verified.",
    "token_endpoint_failed": "The token URL did not issue an access token.",
    "bad_credentials": "The registry rejected the credentials.",
    "forbidden": "The credentials are valid but not allowed to read this registry.",
    "rate_limited": "The registry is rate limiting or temporarily banning this client.",
    "auth_unavailable": "The registry's authentication service is unavailable.",
    "not_registry": "The URL did not respond like an OpenAN Registry Center.",
    "web_page": "The URL returned a web page instead of the registry API.",
    "upstream_error": "The registry returned an unexpected response.",
    "credentials_missing": "The saved credentials are missing. Remove this registry and add it again.",
}

STATUS_KINDS = {401: "bad_credentials", 403: "forbidden", 404: "not_registry", 429: "rate_limited", 503: "auth_unavailable"}

_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="registry-fan-out")
_state_lock = threading.Lock()
_results: dict[tuple, tuple[float, dict]] = {}
_failures: dict[str, tuple[float, str]] = {}
_oauth_tokens: dict[str, tuple[float, str]] = {}
_in_flight: dict[tuple, Future] = {}

test_rate_limit = RateLimiter(max_requests=10, window_seconds=60)


class UpstreamFailure(CanvasPortalError):
    """An external registry call failed. The message is fixed text, never upstream content."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(FAILURE_MESSAGES.get(kind, FAILURE_MESSAGES["upstream_error"]), 502)


@dataclass(frozen=True)
class Source:
    """An external registry. The in-cluster one is represented by source=None."""

    id: str
    name: str
    url: str
    auth_type: str
    skip_tls: bool = False
    token_url: Optional[str] = None
    grant_type: Optional[str] = None
    credentials: Optional[dict] = field(default=None, repr=False)

    @property
    def digest(self) -> str:
        """Identifies this location and credential pair in memory, without keeping them readable."""
        material = json.dumps([self.url, self.token_url, self.grant_type, self.credentials], sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()

    @property
    def lockout_key(self) -> str:
        """Saved registries lock out by id; an unsaved form by what it would send."""
        return self.id or self.digest


class SourceInput(BaseModel):
    name: str = Field(max_length=64)
    url: str = Field(max_length=2048)
    pageUrl: Optional[str] = Field(default=None, max_length=2048)
    authType: Literal["none", "token", "basic", "oauth"]
    skipTlsVerify: bool = False
    allowInsecureHttp: bool = False
    token: Optional[str] = Field(default=None, max_length=8192)
    username: Optional[str] = Field(default=None, max_length=512)
    password: Optional[str] = Field(default=None, max_length=2048)
    tokenUrl: Optional[str] = Field(default=None, max_length=2048)
    grantType: Optional[Literal["client_credentials", "password"]] = None
    clientId: Optional[str] = Field(default=None, max_length=512)
    clientSecret: Optional[str] = Field(default=None, max_length=2048)
    scope: Optional[str] = Field(default=None, max_length=1024)


def _builtin_ref() -> dict:
    return {"id": BUILTIN_ID, "name": BUILTIN_NAME, "builtin": True}


def _source_ref(source: Optional[Source]) -> dict:
    return {"id": source.id, "name": source.name, "builtin": False} if source else _builtin_ref()


def _auth_headers(source: Source) -> dict:
    if source.auth_type == "none":
        return {}
    credentials = source.credentials or {}
    if source.auth_type == "token":
        return {"Authorization": f"Bearer {credentials.get('token', '')}"}
    if source.auth_type == "basic":
        pair = f"{credentials.get('username', '')}:{credentials.get('password', '')}"
        return {"Authorization": f"Basic {base64.b64encode(pair.encode()).decode()}"}
    return {"Authorization": f"Bearer {_oauth_token(source)}"}


def _oauth_token(source: Source) -> str:
    """Exchange the saved credentials at the token URL, reusing the token until shortly before it expires."""
    now = time.monotonic()
    with _state_lock:
        cached = _oauth_tokens.get(source.digest)
    if cached and cached[0] > now:
        return cached[1]

    credentials = source.credentials or {}
    form = {"grant_type": source.grant_type}
    for form_field, key in (
        ("client_id", "clientId"),
        ("client_secret", "clientSecret"),
        ("username", "username"),
        ("password", "password"),
        ("scope", "scope"),
    ):
        if credentials.get(key):
            form[form_field] = credentials[key]
    response = _send(source, "POST", source.token_url or "", data=form)
    if not response.is_success:
        logger.warning("Token request for registry %s returned HTTP %s", source.name, response.status_code)
        raise UpstreamFailure("token_endpoint_failed")
    body = _json_object(response)
    token = body.get("access_token") if body else None
    if not isinstance(token, str) or not token:
        raise UpstreamFailure("token_endpoint_failed")
    expires_in = body.get("expires_in")
    lifetime = expires_in if isinstance(expires_in, (int, float)) and math.isfinite(expires_in) else 60
    lifetime = min(lifetime, MAX_OAUTH_TOKEN_SECONDS)
    if lifetime > 30:
        with _state_lock:
            _oauth_tokens[source.digest] = (now + lifetime - 30, token)
    return token


def _json_object(response: httpx.Response) -> Optional[dict]:
    """Parse a body from an untrusted server, which may be invalid or nested too deeply to parse."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _send(source: Source, method: str, url: str, **kwargs) -> httpx.Response:
    """Call an external URL, reducing every failure to a kind. Exception text is never kept."""
    try:
        return outbound.send(method, url, verify=not source.skip_tls, **kwargs)
    except outbound.InvalidUrl:
        raise UpstreamFailure("invalid_url") from None
    except outbound.BlockedAddress:
        raise UpstreamFailure("blocked") from None
    except outbound.ResolveFailed:
        raise UpstreamFailure("dns_error") from None
    except httpx.TimeoutException:
        raise UpstreamFailure("timeout") from None
    except httpx.TransportError as exc:
        text = str(exc).lower()
        raise UpstreamFailure("tls" if "certificate" in text or "ssl" in text else "unreachable") from None
    except (outbound.UnusableResponse, httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise UpstreamFailure("upstream_error") from None


def _external_get(source: Source, path: str, params: Optional[dict] = None) -> dict:
    response = _send(source, "GET", f"{source.url}{API_BASE}{path}", headers=_auth_headers(source), params=params)
    if not response.is_success:
        logger.warning("Registry %s request to %s returned HTTP %s", source.name, path, response.status_code)
        if response.status_code == 401 and source.auth_type == "oauth":
            # The next attempt must ask for a new token, not resend a rejected one until it expires.
            with _state_lock:
                _oauth_tokens.pop(source.digest, None)
        raise UpstreamFailure(STATUS_KINDS.get(response.status_code, "upstream_error"))
    body = _json_object(response)
    if body is None:
        start = response.content[:256].lstrip().lower()
        raise UpstreamFailure("web_page" if start.startswith((b"<!doctype html", b"<html")) else "not_registry")
    return body


def _registry_get(path: str, params: Optional[dict] = None, source: Optional[Source] = None) -> dict:
    """GET a Registry Center path that returns a JSON object, mapping every failure to a portal error.

    Upstream statuses are never passed through: the frontend's axios client
    treats a 401 as an expired portal session and would log the user out.
    """
    if source is not None:
        return _external_get(source, path, params)
    settings = get_settings()
    base_url = settings.REGISTRY_CENTER_URL.strip().rstrip("/")
    token = settings.REGISTRY_CENTER_TOKEN.strip()
    if not base_url:
        raise CanvasPortalError("Registry Center not configured", 503)
    try:
        resp = httpx.get(
            f"{base_url}{API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning("Registry Center request to %s failed: %s", path, type(exc).__name__)
        raise CanvasPortalError("Registry Center unreachable", 502)
    if not resp.is_success:
        logger.warning("Registry Center request to %s returned HTTP %s", path, resp.status_code)
        raise CanvasPortalError(f"Registry Center returned HTTP {resp.status_code}", 502)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.warning("Registry Center request to %s did not return a JSON object", path)
        raise CanvasPortalError("Registry Center returned an invalid response", 502)
    return body


def _registry_get_optional(path: str, params: Optional[dict] = None, source: Optional[Source] = None) -> Optional[dict]:
    """Like _registry_get, for data the page can render without."""
    try:
        return _registry_get(path, params, source)
    except CanvasPortalError:
        return None


def _health_index(source: Optional[Source] = None) -> tuple[dict[tuple[str, str], dict], bool]:
    """Return ({(organization, name): health entry}, heartbeat enabled)."""
    body = _registry_get_optional("/agents/health", source=source)
    if body is None:
        return {}, False
    entries = body.get("agents")
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    if source is not None:
        entries = [_clean_health(entry) for entry in entries]
    index = {(entry.get("organization"), entry.get("name")): entry for entry in entries}
    config = body.get("config")
    return index, isinstance(config, dict) and config.get("enabled") is True


def _is_shallow(value, limit: int = MAX_PASSTHROUGH_DEPTH) -> bool:
    """Whether passed-through JSON is shallow enough to serialize without exhausting the stack."""
    level = [value]
    for _ in range(limit):
        level = [
            child
            for item in level
            if isinstance(item, (dict, list))
            for child in (item.values() if isinstance(item, dict) else item)
        ]
        if not level:
            return True
    return False


def _strings(value) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _only_text(value: dict, keys: tuple[str, ...]) -> dict:
    return {key: value[key] for key in keys if isinstance(value.get(key), str)}


def _clean_health(entry: dict) -> dict:
    return _only_text(entry, ("organization", "name", "health_status", "last_heartbeat_at", "status_changed_at"))


def _clean_skill(skill) -> Optional[dict]:
    if not isinstance(skill, dict):
        return None
    return {
        **_only_text(skill, ("id", "name", "description")),
        "tags": _strings(skill.get("tags")),
        "examples": _strings(skill.get("examples")),
    }


def _clean_card(card) -> Optional[dict]:
    """Keep the fields the page renders, in the types it expects, from an untrusted registry.

    A card with an object where the page expects text would otherwise break
    rendering for every agent. Cards without a name and organization cannot be
    addressed, so they are dropped.
    """
    if not isinstance(card, dict) or not isinstance(card.get("name"), str):
        return None
    provider = card.get("provider")
    if not isinstance(provider, dict) or not isinstance(provider.get("organization"), str):
        return None
    if not card["name"] or not provider["organization"]:
        return None

    capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
    extensions = capabilities.get("extensions") if isinstance(capabilities.get("extensions"), list) else []
    interfaces = card.get("supportedInterfaces") if isinstance(card.get("supportedInterfaces"), list) else []
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    schemes = card.get("securitySchemes")
    schemes = schemes if isinstance(schemes, dict) and _is_shallow(schemes) else {}
    return {
        **_only_text(card, ("name", "description", "version", "documentationUrl", "iconUrl", "publishStatus")),
        "provider": _only_text(provider, ("organization", "url")),
        "skills": [cleaned for cleaned in map(_clean_skill, skills) if cleaned],
        "supportedInterfaces": [
            _only_text(item, ("url", "protocolBinding", "protocolVersion")) for item in interfaces if isinstance(item, dict)
        ],
        "capabilities": {
            **{key: capabilities.get(key) is True for key in ("streaming", "pushNotifications", "stateTransitionHistory")},
            "extensions": [
                {
                    **_only_text(item, ("uri", "description")),
                    "required": item.get("required") is True,
                    **({"params": item["params"]} if isinstance(item.get("params"), dict) and _is_shallow(item["params"]) else {}),
                }
                for item in extensions
                if isinstance(item, dict)
            ],
        },
        "securitySchemes": schemes,
        "defaultInputModes": _strings(card.get("defaultInputModes")),
        "defaultOutputModes": _strings(card.get("defaultOutputModes")),
    }


def _clean_cards(cards, source: Optional[Source]) -> list[dict]:
    if source is None:
        return cards or []
    return [cleaned for cleaned in map(_clean_card, cards if isinstance(cards, list) else []) if cleaned]


def _discovery_listing(source: Optional[Source]) -> dict:
    discovered = _clean_cards(_registry_get("/agent-cards", source=source).get("agentCards"), source)
    return {
        "agents": [{**card, "publishStatus": "published", "health": None} for card in discovered],
        "statusCounts": {"published": len(discovered)},
        "heartbeatEnabled": False,
        "fullAccess": False,
    }


def _list_agents(full_access: bool, source: Optional[Source] = None) -> dict:
    """List one registry's agents for the caller's role.

    The upstream credential can read admin-only data, so the role is enforced here.
    Non-admins are served only from the registry's public discovery route, which
    applies its own visibility rules; the management route is never read for them.
    An external credential may lack the admin role, which also limits the listing to discovery.
    """
    if not full_access or (source is None and not get_settings().REGISTRY_CENTER_TOKEN.strip()):
        return _discovery_listing(source)
    try:
        body = _registry_get("/agent-cards/manage", source=source)
    except UpstreamFailure as exc:
        if exc.kind != "forbidden":
            raise
        return _discovery_listing(source)
    cards = _clean_cards(body.get("agentCards"), source)
    health, heartbeat_enabled = _health_index(source)
    agents = [
        {**card, "health": health.get(((card.get("provider") or {}).get("organization"), card.get("name")))}
        for card in cards
    ]
    counts = body.get("statusCounts")
    return {
        "agents": agents,
        "statusCounts": counts if isinstance(counts, dict) else {},
        "heartbeatEnabled": heartbeat_enabled,
        "fullAccess": True,
    }


def _fetch_external(source: Source, full_access: bool, refresh: bool = False) -> dict:
    """List an external registry through the cache. Results are kept separately per role tier."""
    if source.credentials is None:
        raise UpstreamFailure("credentials_missing")
    now = time.monotonic()
    with _state_lock:
        failure = _failures.get(source.id)
        if failure and failure[0] > now and (failure[1] in LOCKOUT_KINDS or not refresh):
            raise UpstreamFailure(failure[1])
        cached = _results.get((source.id, full_access))
        if cached and cached[0] > now and not refresh:
            return cached[1]
    try:
        result = _list_agents(full_access, source)
    except UpstreamFailure as exc:
        _record_failure(source, exc.kind)
        raise
    with _state_lock:
        _failures.pop(source.id, None)
        _results[(source.id, full_access)] = (time.monotonic() + CACHE_TTL_SECONDS, result)
    return result


def _record_failure(source: Source, kind: str) -> None:
    now = time.monotonic()
    ttl = LOCKOUT_TTL_SECONDS if kind in LOCKOUT_KINDS else CACHE_TTL_SECONDS
    with _state_lock:
        for key in [key for key, failure in _failures.items() if failure[0] <= now]:
            del _failures[key]
        _failures[source.lockout_key] = (now + ttl, kind)


def _forget_source(source: Source, keep_lockout: bool = False) -> None:
    with _state_lock:
        failure = _failures.get(source.id)
        if not (keep_lockout and failure and failure[1] in LOCKOUT_KINDS):
            _failures.pop(source.id, None)
        _oauth_tokens.pop(source.digest, None)
        for key in [key for key in _results if key[0] == source.id]:
            del _results[key]


def _shared_task(key: tuple, function, *args) -> Future:
    """Run function in the pool, joining the run already in progress for key, if any.

    This limits a slow registry to one running call per key, however many users
    are waiting on it, so it cannot use up the pool and hold up the other registries.
    """
    with _state_lock:
        running = _in_flight.get(key)
        if running is not None:
            return running
        future = _pool.submit(function, *args)
        _in_flight[key] = future

    def finished(_: Future) -> None:
        with _state_lock:
            _in_flight.pop(key, None)

    future.add_done_callback(finished)
    return future


def _builtin_configured() -> bool:
    settings = get_settings()
    return bool(settings.REGISTRY_CENTER_URL.strip())


def _source_from_metadata(metadata: dict, credentials: dict[str, dict]) -> Source:
    return Source(
        id=metadata.get("id", ""),
        name=metadata.get("name", ""),
        url=metadata.get("url", ""),
        auth_type=metadata.get("authType", "token"),
        skip_tls=metadata.get("skipTlsVerify") is True,
        token_url=metadata.get("tokenUrl"),
        grant_type=metadata.get("grantType"),
        credentials=credentials.get(metadata.get("id", "")),
    )


def _external_sources() -> list[Source]:
    """Return the saved registries with their credentials, reading the Secret only if there are any."""
    metadata = registry_store.load_sources()
    if not metadata:
        return []
    credentials = registry_store.read_credentials()
    return [_source_from_metadata(entry, credentials) for entry in metadata]


def _tagged(listing: dict, source: Optional[Source]) -> list[dict]:
    ref = _source_ref(source)
    return [{**agent, "registry": ref} for agent in listing["agents"]]


def _status_entry(source: Optional[Source], listing: Optional[dict], failure: Optional[Exception] = None) -> dict:
    entry = {**_source_ref(source), "status": "ok", "agentCount": 0, "fullAccess": False, "message": None}
    if listing is not None:
        entry.update(agentCount=len(listing["agents"]), fullAccess=listing["fullAccess"])
        return entry
    kind = getattr(failure, "kind", None)
    entry["status"] = "credentials_missing" if kind == "credentials_missing" else "error"
    # Only portal-authored text reaches the browser, whatever the exception held.
    entry["message"] = failure.message if isinstance(failure, CanvasPortalError) else FAILURE_MESSAGES["upstream_error"]
    return entry


def _list_all(full_access: bool, refresh: bool) -> dict:
    """List every registry. One that fails becomes a warning instead of failing the page."""
    store_error = None
    try:
        externals = _external_sources()
    except registry_store.StoreUnavailable as exc:
        externals, store_error = [], exc.message

    if not externals:
        listing = _list_agents(full_access)
        return {
            **listing,
            "agents": _tagged(listing, None),
            "registries": [_status_entry(None, listing)],
            "storeError": store_error,
        }

    tasks = [
        (source, _shared_task((source.id, full_access), _fetch_external, source, full_access, refresh))
        for source in externals
    ]
    if _builtin_configured():
        tasks.insert(0, (None, _shared_task((BUILTIN_ID, full_access), _list_agents, full_access)))
    done, _ = wait([future for _, future in tasks], timeout=FAN_OUT_DEADLINE_SECONDS)

    agents, registries = [], []
    status_counts: dict[str, int] = {}
    heartbeat_enabled = False
    for source, future in tasks:
        if future not in done:
            registries.append(_status_entry(source, None, UpstreamFailure("timeout")))
            continue
        failure = future.exception()
        if failure is not None:
            if not isinstance(failure, CanvasPortalError):
                logger.warning("Listing registry %s failed: %s", _source_ref(source)["name"], type(failure).__name__)
            registries.append(_status_entry(source, None, failure))
            continue
        listing = future.result()
        agents.extend(_tagged(listing, source))
        registries.append(_status_entry(source, listing))
        heartbeat_enabled = heartbeat_enabled or listing["heartbeatEnabled"]
        for status, count in listing["statusCounts"].items():
            if isinstance(count, int):
                status_counts[status] = status_counts.get(status, 0) + count
    return {
        "agents": agents,
        "statusCounts": status_counts,
        "heartbeatEnabled": heartbeat_enabled,
        "fullAccess": full_access,
        "registries": registries,
        "storeError": store_error,
    }


@router.get("/agents")
def get_agents(refresh: bool = False, user: AuthUser = Depends(get_current_user)):
    admin = is_admin(user)
    # Refresh spends the saved credentials on every registry, so it is limited to the people who manage them.
    return _list_all(admin, refresh and admin)


def _agent_detail(source: Optional[Source], organization: str, name: str, user: AuthUser) -> dict:
    # quote() leaves dots alone, so these would rewrite the upstream path below.
    if organization in DOT_SEGMENTS or name in DOT_SEGMENTS:
        raise CanvasPortalError(f"Agent '{organization}/{name}' not found", 404)

    # Find the card in the management list: the public single-card route hides
    # unpublished cards. Doing this first also means the per-agent upstream
    # calls below only ever run for an organization/name known to the registry.
    admin = is_admin(user)
    listing = _fetch_external(source, admin) if source else _list_agents(admin)
    full_access = listing["fullAccess"]
    agent = next(
        (
            item
            for item in listing["agents"]
            if item.get("name") == name and (item.get("provider") or {}).get("organization") == organization
        ),
        None,
    )
    if agent is None:
        raise CanvasPortalError(f"Agent '{organization}/{name}' not found", 404)

    card = dict(agent)
    publish_status = card.pop("publishStatus", None)
    health = card.pop("health", None)
    agent_path = f"{quote(organization, safe='')}/{quote(name, safe='')}"
    # The layer route returns 404 for unpublished cards, so a missing layer is normal.
    registration = _registry_get_optional(f"/agent-cards-with-layer/{agent_path}", source=source)
    review = (
        _registry_get_optional(f"/agent-cards/{agent_path}/review-status", {"limit": REVIEW_HISTORY_LIMIT}, source)
        if full_access
        else None
    )
    layer = registration.get("layer") if registration else None
    if source is not None:
        layer = layer if isinstance(layer, str) else None
        review = _clean_review(review)
    return {
        "card": card,
        "publishStatus": publish_status,
        "health": health,
        "heartbeatEnabled": listing["heartbeatEnabled"],
        "layer": layer,
        "review": review,
        "fullAccess": full_access,
        "registry": _source_ref(source),
    }


def _clean_review(review: Optional[dict]) -> Optional[dict]:
    if review is None:
        return None
    history = review.get("history") if isinstance(review.get("history"), list) else []
    keys = ("from_status", "to_status", "reason", "operator", "at")
    return {
        **_only_text(review, ("status",)),
        "history": [_only_text(entry, keys) for entry in history if isinstance(entry, dict)],
    }


@router.get("/agents/{organization}/{name}")
def get_agent(organization: str, name: str, user: AuthUser = Depends(get_current_user)):
    return _agent_detail(None, organization, name, user)


def _find_source(source_id: str) -> Source:
    source = next((item for item in _external_sources() if item.id == source_id), None)
    if source is None:
        raise CanvasPortalError(f"Registry '{source_id}' not found", 404)
    return source


@router.get("/agents/{registry_id}/{organization}/{name}")
def get_registry_agent(registry_id: str, organization: str, name: str, user: AuthUser = Depends(get_current_user)):
    if registry_id == BUILTIN_ID:
        return _agent_detail(None, organization, name, user)
    source = _find_source(registry_id)
    key = (source.id, is_admin(user), organization, name)
    try:
        return _shared_task(key, _cached_detail, key, source, organization, name, user).result(
            timeout=FAN_OUT_DEADLINE_SECONDS
        )
    except FutureTimeout:
        raise UpstreamFailure("timeout") from None


def _cached_detail(key: tuple, source: Source, organization: str, name: str, user: AuthUser) -> dict:
    with _state_lock:
        cached = _results.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    detail = _agent_detail(source, organization, name, user)
    with _state_lock:
        _results[key] = (time.monotonic() + CACHE_TTL_SECONDS, detail)
    return detail


def _public_source(metadata: dict, credentials_set: bool) -> dict:
    """What an admin sees about a saved registry. Never includes credentials."""
    return {
        "id": metadata.get("id"),
        "name": metadata.get("name"),
        "url": metadata.get("url"),
        "pageUrl": metadata.get("pageUrl"),
        "authType": metadata.get("authType"),
        "grantType": metadata.get("grantType"),
        "tokenUrl": metadata.get("tokenUrl"),
        "skipTlsVerify": metadata.get("skipTlsVerify") is True,
        "createdAt": metadata.get("createdAt"),
        "credentialsSet": credentials_set,
        "builtin": False,
    }


def _page_url(value: Optional[str]) -> Optional[str]:
    """Check a link that admins open in their browser. The backend never calls it, so only its form matters."""
    url = _text(value)
    if not url:
        return None
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise CanvasPortalError("Registry page is not a valid URL", 400) from None
    # Anything but http(s), such as javascript:, would run in the portal's context when clicked.
    if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo:
        raise CanvasPortalError("Registry page must be an http:// or https:// URL without credentials", 400)
    return str(parsed)


def _builtin_page_url() -> Optional[str]:
    try:
        return _page_url(get_settings().REGISTRY_CENTER_PAGE_URL)
    except CanvasPortalError:
        logger.warning("Ignoring REGISTRY_CENTER_PAGE_URL because it is not a valid page URL")
        return None


@router.get("/sources")
def get_sources(user: AuthUser = Depends(get_current_user)):
    if not is_admin(user):
        return {"canManage": False, "storeError": None, "sources": []}
    builtin = (
        [
            {
                **_builtin_ref(),
                "url": get_settings().REGISTRY_CENTER_URL.strip(),
                "pageUrl": _builtin_page_url(),
                "authType": "token",
                "credentialsSet": True,
            }
        ]
        if _builtin_configured()
        else []
    )
    try:
        metadata = registry_store.load_sources()
        credentials = registry_store.read_credentials()
    except registry_store.StoreUnavailable as exc:
        return {"canManage": True, "storeError": exc.message, "sources": builtin}
    return {
        "canManage": True,
        "storeError": None,
        "sources": builtin + [_public_source(entry, entry.get("id") in credentials) for entry in metadata],
    }


def _text(value: Optional[str]) -> str:
    return (value or "").strip()


def _required(value: Optional[str], label: str) -> str:
    # Passwords and tokens are kept as typed: only an all-blank value counts as missing.
    if not (value or "").strip():
        raise CanvasPortalError(f"{label} is required", 400)
    return value


def _normalized_registry_url(url: str) -> str:
    """Accept the host or the full API base, and keep only the base the routes are built on."""
    try:
        parsed = outbound.validate_url(url)
    except outbound.InvalidUrl as exc:
        raise CanvasPortalError(str(exc), 400) from None
    normalized = str(parsed).rstrip("/")
    return normalized[: -len(API_BASE)] if normalized.endswith(API_BASE) else normalized


def _same_destination(body: SourceInput, saved: Source) -> bool:
    """Whether the form sends credentials exactly where, and as safely as, the saved registry does."""
    if body.skipTlsVerify and not saved.skip_tls:
        return False
    try:
        if _normalized_registry_url(body.url) != saved.url:
            return False
        if body.authType != "oauth":
            return True
        return (
            str(outbound.validate_url(body.tokenUrl or "")) == saved.token_url
            and (body.grantType or "client_credentials") == saved.grant_type
        )
    except (CanvasPortalError, outbound.InvalidUrl):
        return False


def _with_saved_credentials(body: SourceInput, saved: Source) -> SourceInput:
    """Fill in the saved credentials when an edit leaves every credential field blank.

    Admins can't read saved credentials, so changing where they go, even to another
    path on a gateway that routes by path, or no longer checking who receives them,
    must require retyping them. Otherwise such an edit could reveal them.
    """
    if saved.credentials is None or body.authType != saved.auth_type:
        return body
    if any(_text(value) for value in (body.token, body.username, body.password, body.clientId, body.clientSecret, body.scope)):
        return body
    if not _same_destination(body, saved):
        return body
    return body.model_copy(
        update={
            key: saved.credentials.get(key)
            for key in ("token", "username", "password", "clientId", "clientSecret", "scope")
        }
    )


def _source_from_input(body: SourceInput, source_id: str = "", saved: Optional[Source] = None) -> Source:
    """Validate a submitted form. Errors name fields only, never submitted values."""
    if saved is not None:
        body = _with_saved_credentials(body, saved)
    name = _text(body.name)
    if not name:
        raise CanvasPortalError("Name is required", 400)
    if name.lower() == BUILTIN_ID:
        raise CanvasPortalError(f"The name '{name}' is reserved", 400)
    url = _normalized_registry_url(_required(body.url, "URL"))

    token_url, grant_type = None, None
    if body.authType == "none":
        credentials = {}
    elif body.authType == "token":
        token = _required(body.token, "Token").strip()
        # Anything else can't be sent in a header, and is usually a paste error.
        if not re.fullmatch(r"[\x21-\x7e]+", token):
            raise CanvasPortalError("Token must not contain spaces or non-ASCII characters", 400)
        credentials = {"token": token}
    elif body.authType == "basic":
        credentials = {"username": _required(body.username, "Username"), "password": _required(body.password, "Password")}
    else:
        grant_type = body.grantType or "client_credentials"
        try:
            token_url = str(outbound.validate_url(_required(body.tokenUrl, "Token URL")))
        except outbound.InvalidUrl as exc:
            raise CanvasPortalError(f"Token URL: {exc}", 400) from None
        if grant_type == "client_credentials":
            credentials = {
                "clientId": _required(body.clientId, "Client ID"),
                "clientSecret": _required(body.clientSecret, "Client secret"),
            }
        else:
            credentials = {
                "username": _required(body.username, "Username"),
                "password": _required(body.password, "Password"),
                **({"clientId": body.clientId} if _text(body.clientId) else {}),
                **({"clientSecret": body.clientSecret} if _text(body.clientSecret) else {}),
            }
        if _text(body.scope):
            credentials["scope"] = _text(body.scope)

    sends_credentials = body.authType != "none"
    if (
        sends_credentials
        and not body.allowInsecureHttp
        and any((value or "").lower().startswith("http://") for value in (url, token_url))
    ):
        raise CanvasPortalError("Credentials would be sent unencrypted over http://; confirm this to continue.", 400)
    return Source(
        id=source_id,
        name=name,
        url=url,
        auth_type=body.authType,
        skip_tls=body.skipTlsVerify,
        token_url=token_url,
        grant_type=grant_type,
        credentials=credentials,
    )


def _source_metadata(source: Source, page_url: Optional[str]) -> dict:
    metadata = {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "authType": source.auth_type,
        "skipTlsVerify": source.skip_tls,
    }
    if page_url:
        metadata["pageUrl"] = page_url
    if source.token_url:
        metadata.update(tokenUrl=source.token_url, grantType=source.grant_type)
    return metadata


@router.post("/sources", status_code=201)
def add_source(body: SourceInput, user: AuthUser = Depends(require_admin)):
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] or "registry"
    source = _source_from_input(body, f"{slug}-{uuid.uuid4().hex[:8]}")
    metadata = {
        **_source_metadata(source, _page_url(body.pageUrl)),
        "createdAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    registry_store.add_source(metadata, source.credentials or {})
    logger.info("User %s added external registry %s", user.username, source.id)
    return _public_source(metadata, True)


@router.put("/sources/{source_id}")
def update_source(source_id: str, body: SourceInput, user: AuthUser = Depends(require_admin)):
    if source_id == BUILTIN_ID:
        raise CanvasPortalError("The in-cluster registry is configured through the portal chart", 400)
    saved = _find_source(source_id)
    source = _source_from_input(body, source_id, saved)
    page_url = _page_url(body.pageUrl)
    credentials = None if source.credentials == saved.credentials else source.credentials or {}
    updated = registry_store.update_source(
        _source_metadata(source, page_url), credentials, _source_metadata(saved, None), saved.credentials
    )
    if updated is None:
        raise CanvasPortalError(f"Registry '{source_id}' not found", 404)
    # Saving unchanged credentials must not lift a lockout, or edits could get around the registry's ban.
    _forget_source(saved, keep_lockout=source.digest == saved.digest)
    logger.info("User %s updated external registry %s", user.username, source_id)
    return _public_source(updated, True)


@router.delete("/sources/{source_id}", status_code=204)
def remove_source(source_id: str, user: AuthUser = Depends(require_admin)):
    if source_id == BUILTIN_ID:
        raise CanvasPortalError("The in-cluster registry is configured through the portal chart", 400)
    source = _find_source(source_id)
    registry_store.remove_source(source_id)
    _forget_source(source)
    logger.info("User %s removed external registry %s", user.username, source_id)
    return Response(status_code=204)


def _test_result(
    classification: str,
    message: str,
    checks: list[dict],
    warnings: Optional[list[str]] = None,
    level: Optional[str] = None,
    agent_count: Optional[int] = None,
) -> dict:
    return {
        "ok": classification == "ok",
        "classification": classification,
        "message": message,
        "level": level,
        "agentCount": agent_count,
        "checks": checks,
        "warnings": warnings or [],
    }


def _failed_test(source: Source, failure: UpstreamFailure, checks: list[dict], check: str) -> dict:
    checks.append({"name": check, "status": "fail"})
    warnings = []
    if failure.kind in LOCKOUT_KINDS:
        # A retry with the same credentials could trigger the registry's ban.
        _record_failure(source, failure.kind)
        warnings.append("Repeated failed logins can get this portal banned by the registry for 5 minutes; fix the credentials before retrying.")
    if failure.kind == "web_page":
        parsed = httpx.URL(source.url)
        origin = f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"
        warnings.append(f"If this is an OpenAN Portal, its registry API is usually at {origin}{PORTAL_REGISTRY_PATH}.")
    if failure.kind == "bad_credentials" and source.auth_type == "none":
        warnings.append("This registry requires credentials.")
    if failure.kind == "bad_credentials" and source.auth_type == "basic":
        warnings.append("OpenAN Registry Center accepts only Bearer tokens, so Basic credentials work only through a proxy that accepts them.")
    if failure.kind == "tls":
        warnings.append("If this registry uses a self-signed certificate, you can turn on 'Skip TLS verification'.")
    return _test_result(failure.kind, failure.message, checks, warnings)


def _run_test(source: Source) -> dict:
    """Check the registry one step at a time, without retries, and stop at the first conclusive failure."""
    checks: list[dict] = []
    now = time.monotonic()
    with _state_lock:
        failure = _failures.get(source.lockout_key)
    if failure and failure[0] > now and failure[1] in LOCKOUT_KINDS:
        return _test_result(
            "cooldown",
            f"These credentials were just rejected. Try again in {int(failure[0] - now) + 1} seconds, or change them.",
            checks,
        )

    # No credentials are sent until the host answers, so a bad address or certificate exposes nothing.
    try:
        health = _send(source, "GET", f"{source.url}/health")
        checks.append({"name": "health", "status": "ok" if health.is_success else "warn", "httpStatus": health.status_code})
    except UpstreamFailure as exc:
        return _failed_test(source, exc, checks, "health")

    if source.auth_type == "oauth":
        try:
            _oauth_token(source)
            checks.append({"name": "token", "status": "ok"})
        except UpstreamFailure as exc:
            return _failed_test(source, exc, checks, "token")

    try:
        discovery = _external_get(source, "/agent-cards")
    except UpstreamFailure as exc:
        return _failed_test(source, exc, checks, "discovery")
    cards = discovery.get("agentCards")
    if not isinstance(cards, list):
        return _failed_test(source, UpstreamFailure("not_registry"), checks, "discovery")
    checks.append({"name": "discovery", "status": "ok"})

    warnings = []
    level = None
    try:
        _external_get(source, "/agent-cards/manage")
        level = "admin"
        checks.append({"name": "manage", "status": "ok"})
    except UpstreamFailure as exc:
        if exc.kind in LOCKOUT_KINDS or exc.kind == "auth_unavailable":
            return _failed_test(source, exc, checks, "manage")
        checks.append({"name": "manage", "status": "warn"})
        if exc.kind == "forbidden":
            level = "discovery"
            warnings.append("These credentials cannot read all review states, so only published agents will be listed.")
        else:
            warnings.append("Could not check whether these credentials can read all review states.")
    count = len(cards)
    return _test_result(
        "ok",
        f"Connected. {count} published agent{'' if count == 1 else 's'} found.",
        checks,
        warnings,
        level=level,
        agent_count=count,
    )


@router.post("/sources/test", dependencies=[Depends(require_admin), Depends(test_rate_limit)])
def test_source(body: SourceInput):
    return _test_unsaved(_source_from_input(body))


def _test_unsaved(source: Source) -> dict:
    try:
        return _run_test(source)
    finally:
        # Nothing was saved, so a token issued for this test must not stay in memory.
        with _state_lock:
            _oauth_tokens.pop(source.digest, None)


@router.post("/sources/{source_id}/test", dependencies=[Depends(require_admin), Depends(test_rate_limit)])
def test_saved_source(source_id: str, body: Optional[SourceInput] = None):
    """Test a saved registry, or with a body, the edits being made to it before they are saved."""
    source = _find_source(source_id)
    if body is not None:
        edited = _source_from_input(body, saved=source)
        # With the same address and credentials, the saved registry's lockout still applies.
        return _run_test(replace(edited, id=source.id)) if edited.digest == source.digest else _test_unsaved(edited)
    if source.credentials is None:
        return _test_result("credentials_missing", FAILURE_MESSAGES["credentials_missing"], [])
    result = _run_test(source)
    if result["ok"]:
        _forget_source(source)
    return result
