"""Read-only proxy to the OpenAN Registry Center.

The browser cannot call the Registry Center directly (CSP ``connect-src 'self'``,
no CORS upstream, and the token must stay server-side). This router only ever
sends GET requests, even though the configured credential is an admin one.
"""
import logging
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends

from auth.middleware import get_current_user, is_admin
from auth.models import AuthUser
from config import get_settings
from utils.exceptions import CanvasPortalError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registries", tags=["registries"])

API_BASE = "/rest/v1/registry-center"
TIMEOUT_SECONDS = 5.0
REVIEW_HISTORY_LIMIT = 100
DOT_SEGMENTS = {".", ".."}


def _registry_get(path: str, params: Optional[dict] = None) -> dict:
    """GET a Registry Center path that returns a JSON object, mapping every failure to a portal error.

    Upstream statuses are never passed through: the frontend's axios client
    treats a 401 as an expired portal session and would log the user out.
    """
    settings = get_settings()
    base_url = settings.REGISTRY_CENTER_URL.strip().rstrip("/")
    token = settings.REGISTRY_CENTER_TOKEN.strip()
    if not base_url or not token:
        raise CanvasPortalError("Registry Center not configured", 503)
    try:
        resp = httpx.get(
            f"{base_url}{API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
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


def _registry_get_optional(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Like _registry_get, for data the page can render without."""
    try:
        return _registry_get(path, params)
    except CanvasPortalError:
        return None


def _health_index() -> tuple[dict[tuple[str, str], dict], bool]:
    """Return ({(organization, name): health entry}, heartbeat enabled)."""
    body = _registry_get_optional("/agents/health")
    if body is None:
        return {}, False
    index = {(entry.get("organization"), entry.get("name")): entry for entry in body.get("agents") or []}
    return index, (body.get("config") or {}).get("enabled") is True


def _list_agents(full_access: bool) -> dict:
    """List agents for the caller's role.

    The upstream token can read admin-only data, so the role is enforced here.
    Non-admins are served only from the registry's public discovery route, which
    applies its own visibility rules; the management route is never read for them.
    """
    if not full_access:
        discovered = _registry_get("/agent-cards").get("agentCards") or []
        return {
            "agents": [{**card, "publishStatus": "published", "health": None} for card in discovered],
            "statusCounts": {"published": len(discovered)},
            "heartbeatEnabled": False,
            "fullAccess": False,
        }
    body = _registry_get("/agent-cards/manage")
    cards = body.get("agentCards") or []
    health, heartbeat_enabled = _health_index()
    agents = [
        {**card, "health": health.get(((card.get("provider") or {}).get("organization"), card.get("name")))}
        for card in cards
    ]
    return {
        "agents": agents,
        "statusCounts": body.get("statusCounts") or {},
        "heartbeatEnabled": heartbeat_enabled,
        "fullAccess": True,
    }


@router.get("/agents")
def get_agents(user: AuthUser = Depends(get_current_user)):
    return _list_agents(is_admin(user))


@router.get("/agents/{organization}/{name}")
def get_agent(organization: str, name: str, user: AuthUser = Depends(get_current_user)):
    # quote() leaves dots alone, so these would rewrite the upstream path below.
    if organization in DOT_SEGMENTS or name in DOT_SEGMENTS:
        raise CanvasPortalError(f"Agent '{organization}/{name}' not found", 404)

    # Find the card in the management list: the public single-card route hides
    # unpublished cards. Doing this first also means the per-agent upstream
    # calls below only ever run for an organization/name known to the registry.
    full_access = is_admin(user)
    listing = _list_agents(full_access)
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
    registration = _registry_get_optional(f"/agent-cards-with-layer/{agent_path}")
    review = (
        _registry_get_optional(f"/agent-cards/{agent_path}/review-status", {"limit": REVIEW_HISTORY_LIMIT})
        if full_access
        else None
    )
    return {
        "card": card,
        "publishStatus": publish_status,
        "health": health,
        "heartbeatEnabled": listing["heartbeatEnabled"],
        "layer": registration.get("layer") if registration else None,
        "review": review,
        "fullAccess": full_access,
    }
