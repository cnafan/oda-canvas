"""Register a component's live Agent Card with the built-in Registry Center."""
import json
import re
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends
from kubernetes import client
from pydantic import BaseModel, Field

from auth.middleware import get_current_user, require_admin
from auth.models import AuthUser
from config import get_settings
from k8s.impersonation import get_impersonated_client
from k8s.resources.components import get_component
from routers import registries
from utils.exceptions import CanvasPortalError

router = APIRouter(prefix="/api/components", tags=["component-registry"])
CARD_ANNOTATION = "openan.org/agent-card-url"
MAX_CARD_BYTES = 256 * 1024
SERVICE_HOST = re.compile(r"^([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)\.([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)\.svc\.cluster\.local$")


class RegistrationRequest(BaseModel):
    layer: Literal["service", "network", "unknown"]
    name: str = Field(min_length=1, max_length=256)
    organization: str = Field(min_length=1, max_length=256)


class IdentityRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    organization: str = Field(min_length=1, max_length=256)


def _card_for_component(namespace: str, name: str, user: AuthUser) -> dict:
    component = get_component(name, namespace, user)
    url = (component.get("metadata", {}).get("annotations") or {}).get(CARD_ANNOTATION, "")
    try:
        parsed = urlsplit(url)
        match = SERVICE_HOST.fullmatch(parsed.hostname or "")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        match = None
        port = 0
    if not match or parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CanvasPortalError("Agent Card URL must point to a Kubernetes Service in this cluster", 400)
    service_name, service_namespace = match.groups()
    service = client.CoreV1Api(get_impersonated_client(user)).read_namespaced_service(service_name, service_namespace)
    if service.spec.type == "ExternalName" or not service.spec.cluster_ip or service.spec.cluster_ip == "None":
        raise CanvasPortalError("Agent Card Service must have a cluster IP", 400)
    if not any(item.port == port for item in (service.spec.ports or [])):
        raise CanvasPortalError("Agent Card URL port is not exposed by its Service", 400)
    try:
        with httpx.Client(timeout=5.0, trust_env=False, follow_redirects=False) as http:
            with http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise CanvasPortalError("Unable to read the Agent Card from its Service", 502)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CARD_BYTES:
                        raise CanvasPortalError("Agent Card is too large", 502)
        card = json.loads(body)
    except (httpx.HTTPError, ValueError) as exc:
        raise CanvasPortalError("Unable to read a valid Agent Card from its Service", 502) from exc
    if not isinstance(card, dict) or not isinstance(card.get("name"), str) or not card["name"].strip() or not isinstance(card.get("provider"), dict) or not isinstance(card["provider"].get("organization"), str) or not card["provider"]["organization"].strip():
        raise CanvasPortalError("Agent Card must include a name and provider organization", 502)
    return card


def _status(card: dict) -> dict:
    identity = {"name": card["name"], "organization": card["provider"]["organization"]}
    body = registries._registry_get("/agent-cards/manage")
    cards = body.get("agentCards")
    if not isinstance(cards, list):
        raise CanvasPortalError("Registry Center returned an invalid response", 502)
    for item in cards:
        if isinstance(item, dict) and item.get("name") == identity["name"] and (item.get("provider") or {}).get("organization") == identity["organization"]:
            return {**identity, "status": item.get("publishStatus") or "unknown"}
    return {**identity, "status": "unregistered"}


def _check_identity(card: dict, expected: IdentityRequest | RegistrationRequest) -> None:
    if card["name"] != expected.name or card["provider"]["organization"] != expected.organization:
        raise CanvasPortalError("Agent Card identity changed; refresh the status and try again", 409)


def _write_registry(method: str, path: str, payload: dict | None = None) -> None:
    settings = get_settings()
    base = settings.REGISTRY_CENTER_URL.strip().rstrip("/")
    if not base:
        raise CanvasPortalError("Registry Center not configured", 503)
    token = settings.REGISTRY_CENTER_TOKEN.strip()
    try:
        response = httpx.request(method, f"{base}{registries.API_BASE}{path}", json=payload,
                                 headers={"Authorization": f"Bearer {token}"} if token else {},
                                 timeout=10.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise CanvasPortalError("Registry Center unreachable", 502) from exc
    if not response.is_success:
        raise CanvasPortalError(f"Registry Center returned HTTP {response.status_code}", 502)
    with registries._state_lock:
        registries._results.clear()


@router.get("/{namespace}/{name}/registry-status")
def component_registry_status(namespace: str, name: str, user: AuthUser = Depends(get_current_user)):
    return _status(_card_for_component(namespace, name, user))


@router.post("/{namespace}/{name}/register")
def register_component(namespace: str, name: str, body: RegistrationRequest, user: AuthUser = Depends(require_admin)):
    card = _card_for_component(namespace, name, user)
    _check_identity(card, body)
    if _status(card)["status"] != "unregistered":
        raise CanvasPortalError("Agent is already registered; refresh the status", 409)
    _write_registry("POST", "/agent-cards", {"agentCards": [{"agentCard": card, "layer": body.layer}]})
    return _status(card)


@router.post("/{namespace}/{name}/deregister")
def deregister_component(namespace: str, name: str, body: IdentityRequest, user: AuthUser = Depends(require_admin)):
    card = _card_for_component(namespace, name, user)
    _check_identity(card, body)
    if _status(card)["status"] == "unregistered":
        raise CanvasPortalError("Agent is not registered; refresh the status", 409)
    org = quote(body.organization, safe="")
    agent = quote(body.name, safe="")
    _write_registry("DELETE", f"/agent-cards/{org}/{agent}")
    return {"name": body.name, "organization": body.organization, "status": "unregistered"}
