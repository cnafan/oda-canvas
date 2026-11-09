"""Tests for the read-only Registry Center proxy, with the upstream stubbed."""
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.middleware import get_current_user, is_admin
from auth.models import AuthUser
from config import get_settings
from routers import registries
from utils.exceptions import register_exception_handlers

TOKEN = "test-registry-token"
API = "/rest/v1/registry-center"

REGISTERED = {"name": "A b", "provider": {"organization": "org"}, "publishStatus": "registered"}
PUBLISHED = {"name": "B", "provider": {"organization": "org"}, "publishStatus": "published"}
MANAGE = (200, {"agentCards": [REGISTERED, PUBLISHED], "statusCounts": {"registered": 1, "published": 1}})
DISCOVERY = (200, {"agentCards": [{"name": "B", "provider": {"organization": "org"}}]})
HEALTH = (
    200,
    {"agents": [{"name": "B", "organization": "org", "health_status": "healthy"}], "config": {"enabled": True}},
)


ADMIN = AuthUser(username="admin", email="", groups=["/canvas-admin"])
VIEWER = AuthUser(username="viewer", email="", groups=["/viewers"])

app = FastAPI()
register_exception_handlers(app)
app.include_router(registries.router)
client = TestClient(app, raise_server_exceptions=False)


def act_as(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("REGISTRY_CENTER_URL", "http://rc:5000/")
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", TOKEN)
    act_as(ADMIN)
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def upstream(monkeypatch):
    """Serve a {path: (status, json) | Exception} map in place of the registry; returns the calls made."""

    def install(routes=None):
        table = {"/agent-cards/manage": MANAGE, "/agent-cards": DISCOVERY, "/agents/health": HEALTH, **(routes or {})}
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "params": params, "headers": headers})
            result = table.get(url.split(API, 1)[1], (404, {}))
            if isinstance(result, Exception):
                raise result
            status, body = result
            return httpx.Response(status, json=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        return calls

    return install


def test_list_returns_every_status_and_merges_health(upstream):
    calls = upstream()

    body = client.get("/api/registries/agents").json()

    assert [agent["publishStatus"] for agent in body["agents"]] == ["registered", "published"]
    assert body["agents"][0]["health"] is None
    assert body["agents"][1]["health"]["health_status"] == "healthy"
    assert body["heartbeatEnabled"] is True
    assert body["statusCounts"] == {"registered": 1, "published": 1}
    assert calls[0]["url"] == f"http://rc:5000{API}/agent-cards/manage"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_health_failure_does_not_fail_the_list(upstream):
    upstream({"/agents/health": (403, {})})

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    assert response.json()["heartbeatEnabled"] is False


@pytest.mark.parametrize("status", [401, 403, 500])
def test_upstream_error_becomes_502_and_never_401(upstream, status):
    upstream({"/agent-cards/manage": (status, {})})

    response = client.get("/api/registries/agents")

    assert response.status_code == 502
    assert TOKEN not in response.text


def test_unreachable_upstream_becomes_502_without_logging_the_token(upstream, caplog):
    upstream({"/agent-cards/manage": httpx.ConnectError("refused")})

    with caplog.at_level(logging.DEBUG):
        response = client.get("/api/registries/agents")

    assert response.status_code == 502
    assert TOKEN not in caplog.text


def test_non_object_json_becomes_502(upstream):
    upstream({"/agent-cards/manage": (200, ["not", "an", "object"])})

    assert client.get("/api/registries/agents").status_code == 502


def test_not_configured_is_503(monkeypatch):
    monkeypatch.setenv("REGISTRY_CENTER_URL", "")
    get_settings.cache_clear()

    response = client.get("/api/registries/agents")

    assert response.status_code == 503
    assert response.json()["detail"] == "Registry Center not configured"


def test_builtin_without_token_uses_public_discovery_even_for_admin(monkeypatch, upstream):
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", "")
    get_settings.cache_clear()
    calls = upstream()

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    assert response.json()["fullAccess"] is False
    assert [agent["publishStatus"] for agent in response.json()["agents"]] == ["published"]
    assert [call["url"] for call in calls] == [f"http://rc:5000{API}/agent-cards"]
    assert calls[0]["headers"] == {}


def test_detail_of_unpublished_agent_without_layer(upstream):
    upstream(
        {
            "/agent-cards/org/A%20b/review-status": (
                200,
                {"status": "registered", "history": [{"to_status": "registered", "at": "t"}]},
            )
        }
    )

    response = client.get("/api/registries/agents/org/A%20b")

    body = response.json()
    assert response.status_code == 200
    assert body["publishStatus"] == "registered"
    assert body["layer"] is None
    assert body["review"]["history"][0]["at"] == "t"
    assert "publishStatus" not in body["card"]
    assert "health" not in body["card"]


def test_detail_of_published_agent_with_layer_and_no_review(upstream):
    upstream({"/agent-cards-with-layer/org/B": (200, {"agentCard": {}, "layer": "service"})})

    body = client.get("/api/registries/agents/org/B").json()

    assert body["layer"] == "service"
    assert body["health"]["health_status"] == "healthy"
    assert body["review"] is None


def test_unknown_agent_is_404_without_per_agent_upstream_calls(upstream):
    calls = upstream()

    response = client.get("/api/registries/agents/%2e%2e/manage")

    assert response.status_code == 404
    assert not [call for call in calls if "with-layer" in call["url"] or "review-status" in call["url"]]


def test_card_named_with_dot_segment_never_reaches_an_upstream_path(upstream):
    dotted = {"name": "..", "provider": {"organization": "org"}, "publishStatus": "published"}
    calls = upstream({"/agent-cards/manage": (200, {"agentCards": [dotted]})})

    response = client.get("/api/registries/agents/org/%2e%2e")

    assert response.status_code == 404
    assert calls == []


def test_non_admin_list_has_only_published_agents_and_no_health(upstream):
    calls = upstream()
    act_as(VIEWER)

    body = client.get("/api/registries/agents").json()

    assert [agent["name"] for agent in body["agents"]] == ["B"]
    assert body["agents"][0]["health"] is None
    assert body["statusCounts"] == {"published": 1}
    assert body["heartbeatEnabled"] is False
    assert body["fullAccess"] is False
    assert not [call for call in calls if "agents/health" in call["url"]]


def test_non_admin_is_served_only_from_public_discovery(upstream):
    calls = upstream(
        {"/agent-cards/manage": (200, {"agentCards": [{**PUBLISHED, "adminOnly": "secret"}]})}
    )
    act_as(VIEWER)

    response = client.get("/api/registries/agents")

    assert "secret" not in response.text
    assert response.json()["agents"][0]["publishStatus"] == "published"
    assert [call["url"].split(API, 1)[1] for call in calls] == ["/agent-cards"]


def test_non_admin_does_not_see_a_published_agent_discovery_hides(upstream):
    upstream({"/agent-cards": (200, {"agentCards": []})})
    act_as(VIEWER)

    assert client.get("/api/registries/agents").json()["agents"] == []
    assert client.get("/api/registries/agents/org/B").status_code == 404


def test_non_admin_cannot_tell_an_unpublished_agent_from_a_missing_one(upstream):
    calls = upstream()
    act_as(VIEWER)

    unpublished = client.get("/api/registries/agents/org/A%20b")
    missing = client.get("/api/registries/agents/org/Nope")

    assert unpublished.status_code == missing.status_code == 404
    assert "Nope" in missing.json()["detail"] and "A b" in unpublished.json()["detail"]
    assert not [call for call in calls if "with-layer" in call["url"] or "review-status" in call["url"]]


def test_non_admin_detail_of_published_agent_has_no_review_or_health(upstream):
    calls = upstream({"/agent-cards-with-layer/org/B": (200, {"layer": "service"})})
    act_as(VIEWER)

    body = client.get("/api/registries/agents/org/B").json()

    assert body["layer"] == "service"
    assert body["review"] is None
    assert body["health"] is None
    assert body["fullAccess"] is False
    assert not [call for call in calls if "review-status" in call["url"] or "agents/health" in call["url"]]


def test_admin_responses_report_full_access(upstream):
    upstream()

    assert client.get("/api/registries/agents").json()["fullAccess"] is True
    assert client.get("/api/registries/agents/org/A%20b").json()["fullAccess"] is True


@pytest.mark.parametrize(
    "admin_groups, allow_all, expected",
    [
        ('["canvas-admin"]', "false", False),
        ('["viewers"]', "false", True),
        ('[]', "false", False),
        ('[]', "true", True),
        ('["canvas-admin"]', "true", False),
    ],
)
def test_is_admin_follows_the_require_admin_rule(monkeypatch, admin_groups, allow_all, expected):
    monkeypatch.setenv("AUTHZ_ADMIN_GROUPS", admin_groups)
    monkeypatch.setenv("AUTHZ_ALLOW_ALL_AUTHENTICATED", allow_all)
    get_settings.cache_clear()

    assert is_admin(VIEWER) is expected


def test_routes_require_authentication():
    app.dependency_overrides.clear()

    assert client.get("/api/registries/agents").status_code in (401, 403)
    assert client.get("/api/registries/agents/org/B").status_code in (401, 403)
