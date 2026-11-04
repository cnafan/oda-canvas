"""Tests for external registries: management, connection test, listing and the network guard."""
import base64
import gzip
import json
import logging
import socket
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from auth.middleware import get_current_user
from auth.models import AuthUser
from config import get_settings
from k8s import registry_store
from routers import registries
from utils import outbound
from utils.exceptions import register_exception_handlers

BASE = "https://registry.example.com"
API = "/rest/v1/registry-center"
SECRET = "s3cret-value"
ADMIN = AuthUser(username="admin", email="", groups=["/canvas-admin"])
VIEWER = AuthUser(username="viewer", email="", groups=["/viewers"])

CARD = {"name": "Remote", "provider": {"organization": "org"}, "skills": [{"id": "s", "name": "Skill"}]}
MANAGED = {**CARD, "publishStatus": "registered"}
TOKEN_FORM = {"name": "Partner", "url": BASE, "authType": "token", "token": SECRET}

app = FastAPI()
register_exception_handlers(app)
app.include_router(registries.router)
client = TestClient(app, raise_server_exceptions=False)


def act_as(user):
    app.dependency_overrides[get_current_user] = lambda: user


class FakeSecret:
    """Stands in for the Secret, using the merge semantics confirmed on a real cluster."""

    def __init__(self):
        self.data = {}
        self.error = None
        self.patches = []

    def read_namespaced_secret(self, name, namespace):
        if self.error:
            raise self.error
        return type("Secret", (), {"data": dict(self.data)})()

    def patch_namespaced_secret(self, name, namespace, body):
        if self.error:
            raise self.error
        self.patches.append(body)
        for key, value in body.get("stringData", {}).items():
            self.data[key] = base64.b64encode(value.encode()).decode()
        for key, value in body.get("data", {}).items():
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    get_settings.cache_clear()
    monkeypatch.setenv("REGISTRY_CENTER_URL", "")
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", "")
    monkeypatch.setenv("EXTERNAL_REGISTRY_CATALOG_PATH", str(tmp_path / "registries.json"))
    monkeypatch.setenv("REGISTRY_CREDENTIALS_SECRET", "creds")
    monkeypatch.setenv("REGISTRY_CREDENTIALS_NAMESPACE", "portal")
    for state in (registries._results, registries._failures, registries._oauth_tokens, registries._in_flight):
        state.clear()
    registries.test_rate_limit._hits.clear()
    act_as(ADMIN)
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def secret(monkeypatch):
    fake = FakeSecret()
    monkeypatch.setattr(registry_store, "_core_api", lambda: fake)
    return fake


@pytest.fixture
def upstream(monkeypatch):
    """Serve {path after the base URL: (status, json) | Exception} in place of outbound.send."""

    def install(routes=None, base=BASE):
        table = {
            "/health": (200, {"status": "ok"}),
            f"{API}/agent-cards": (200, {"agentCards": [CARD]}),
            f"{API}/agent-cards/manage": (200, {"agentCards": [MANAGED], "statusCounts": {"registered": 1}}),
            f"{API}/agents/health": (200, {"agents": [], "config": {"enabled": False}}),
            **(routes or {}),
        }
        calls = []

        def fake_send(method, url, headers=None, params=None, data=None, verify=True):
            path = url.split(base, 1)[1] if url.startswith(base) else url
            calls.append({"method": method, "path": path, "headers": headers, "data": data, "verify": verify})
            result = table.get(path, (404, {}))
            if isinstance(result, Exception):
                raise result
            status, body = result
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

        monkeypatch.setattr(outbound, "send", fake_send)
        return calls

    return install


def add(form=TOKEN_FORM):
    response = client.post("/api/registries/sources", json=form)
    assert response.status_code == 201, response.text
    return response.json()


# --- Credentials stay private ------------------------------------------------


@pytest.mark.parametrize(
    "form",
    [
        TOKEN_FORM,
        {"name": "B", "url": BASE, "authType": "basic", "username": "u", "password": SECRET},
        {
            "name": "C",
            "url": BASE,
            "authType": "oauth",
            "tokenUrl": "https://iam.example.com/token",
            "clientId": "id",
            "clientSecret": SECRET,
        },
        {
            "name": "D",
            "url": BASE,
            "authType": "oauth",
            "grantType": "password",
            "tokenUrl": "https://iam.example.com/token",
            "username": "u",
            "password": SECRET,
        },
    ],
)
def test_credentials_only_ever_reach_the_secret(secret, upstream, caplog, tmp_path, form):
    upstream({"https://iam.example.com/token": (200, {"access_token": "issued", "expires_in": 300})})

    with caplog.at_level(logging.DEBUG):
        created = client.post("/api/registries/sources", json=form)
        listed = client.get("/api/registries/sources")
        agents = client.get("/api/registries/agents")
        tested = client.post(f"/api/registries/sources/{created.json()['id']}/test")

    for response in (created, listed, agents, tested):
        assert response.status_code in (200, 201), response.text
        assert SECRET not in response.text
    assert SECRET not in caplog.text
    assert SECRET not in (tmp_path / "registries.json").read_text()
    assert SECRET in json.dumps(secret.patches)
    assert listed.json()["sources"][0]["credentialsSet"] is True


def test_validation_errors_do_not_echo_submitted_credentials(secret):
    for path in ("/api/registries/sources", "/api/registries/sources/test"):
        response = client.post(path, json={**TOKEN_FORM, "authType": "nope", "password": SECRET})
        assert response.status_code == 422
        assert SECRET not in response.text
        assert "input" not in response.json()["detail"][0]


def test_test_messages_do_not_echo_exception_text(secret, upstream):
    upstream({"/health": httpx.ConnectError(f"failed with {SECRET}")})

    response = client.post("/api/registries/sources/test", json=TOKEN_FORM)

    assert response.json()["classification"] == "unreachable"
    assert SECRET not in response.text


# --- Access control ----------------------------------------------------------


def test_non_admins_cannot_manage_or_test(secret, upstream):
    calls = upstream()
    created = add()
    act_as(VIEWER)

    assert client.post("/api/registries/sources", json=TOKEN_FORM).status_code == 403
    assert client.delete(f"/api/registries/sources/{created['id']}").status_code == 403
    assert client.post("/api/registries/sources/test", json=TOKEN_FORM).status_code == 403
    assert client.post(f"/api/registries/sources/{created['id']}/test").status_code == 403
    assert calls == []


def test_non_admins_see_no_source_details(secret):
    add()
    act_as(VIEWER)

    assert client.get("/api/registries/sources").json() == {"canManage": False, "storeError": None, "sources": []}


def test_non_admin_listing_uses_only_discovery_and_the_cache_does_not_leak_admin_data(secret, upstream):
    calls = upstream()
    add()

    assert client.get("/api/registries/agents").json()["agents"][0]["publishStatus"] == "registered"
    act_as(VIEWER)
    calls.clear()
    viewer = client.get("/api/registries/agents").json()

    assert viewer["agents"][0]["publishStatus"] == "published"
    assert viewer["fullAccess"] is False
    assert [call["path"] for call in calls] == [f"{API}/agent-cards"]


# --- Add and remove ----------------------------------------------------------


def test_remove_deletes_metadata_and_credentials(secret, upstream):
    upstream()
    created = add()
    assert created["id"] in secret.data

    assert client.delete(f"/api/registries/sources/{created['id']}").status_code == 204

    assert secret.data == {}
    assert client.get("/api/registries/sources").json()["sources"] == []


def test_in_cluster_registry_cannot_be_removed(secret):
    assert client.delete("/api/registries/sources/in-cluster").status_code == 400


def test_removing_an_unknown_registry_is_404(secret):
    assert client.delete("/api/registries/sources/nope").status_code == 404


@pytest.mark.parametrize("name", ["PARTNER", "partner"])
def test_duplicate_name_is_409(secret, name):
    add()

    assert client.post("/api/registries/sources", json={**TOKEN_FORM, "name": name}).status_code == 409


@pytest.mark.parametrize("name", ["in-cluster", "In-Cluster"])
def test_reserved_name_is_refused(secret, name):
    assert client.post("/api/registries/sources", json={**TOKEN_FORM, "name": name}).status_code == 400


def test_full_api_base_url_is_normalized(secret):
    created = add({**TOKEN_FORM, "url": f"{BASE}{API}/"})

    assert created["url"] == BASE


@pytest.mark.parametrize(
    "form, field",
    [
        ({"authType": "token", "token": " "}, "Token"),
        ({"authType": "basic", "username": "u"}, "Password"),
        ({"authType": "oauth", "clientId": "c", "clientSecret": "s"}, "Token URL"),
        ({"authType": "oauth", "tokenUrl": "https://i.example.com/t", "clientId": "c"}, "Client secret"),
        ({"authType": "oauth", "tokenUrl": "https://i.example.com/t", "grantType": "password", "username": "u"}, "Password"),
    ],
)
def test_missing_credentials_are_refused(secret, form, field):
    response = client.post("/api/registries/sources", json={"name": "N", "url": BASE, **form})

    assert response.status_code == 400
    assert field in response.json()["detail"]


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1", "http://[::1]", "http://169.254.169.254", "http://rc.components.svc", "http://rc.ns.svc.cluster.local", "http://localhost", "http://single", "ftp://a.example.com", "https://u:p@a.example.com", "https://a.example.com/?x=1"]
)
def test_disallowed_urls_are_refused_on_save_and_test(secret, url):
    form = {**TOKEN_FORM, "url": url, "allowInsecureHttp": True}

    assert client.post("/api/registries/sources", json=form).status_code == 400
    assert client.post("/api/registries/sources/test", json=form).status_code == 400


def test_registry_page_is_saved_and_returned(secret):
    page = "https://portal.example.com/registry?tab=agents#list"

    created = add({**TOKEN_FORM, "pageUrl": f" {page} "})

    assert created["pageUrl"] == page
    assert client.get("/api/registries/sources").json()["sources"][0]["pageUrl"] == page


@pytest.mark.parametrize("page", [None, "", "   "])
def test_registry_page_is_optional(secret, page):
    assert add({**TOKEN_FORM, "pageUrl": page})["pageUrl"] is None


@pytest.mark.parametrize(
    "page",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,x",
        "ftp://portal.example.com",
        "https:///registry",
        "portal.example.com/registry",
        "https://u:p@portal.example.com",
    ],
)
def test_unsafe_registry_pages_are_refused_and_nothing_is_saved(secret, page):
    response = client.post("/api/registries/sources", json={**TOKEN_FORM, "pageUrl": page})

    assert response.status_code == 400
    assert page not in response.text
    assert client.get("/api/registries/sources").json()["sources"] == []
    assert secret.data == {}


def test_registry_page_is_never_called(secret, upstream):
    calls = upstream()
    page = "https://portal.example.com/registry"
    add({**TOKEN_FORM, "pageUrl": page})

    client.post(f"/api/registries/sources/{client.get('/api/registries/sources').json()['sources'][0]['id']}/test")
    client.get("/api/registries/agents?refresh=true")

    assert calls and not any("portal.example.com" in call["path"] for call in calls)


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("", None),
        ("http://10.0.0.1:8082/registry", "http://10.0.0.1:8082/registry"),
        ("javascript:alert(1)", None),
    ],
)
def test_in_cluster_registry_page_comes_from_the_chart(secret, monkeypatch, configured, expected):
    monkeypatch.setenv("REGISTRY_CENTER_URL", "http://rc:5000")
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", "tok")
    monkeypatch.setenv("REGISTRY_CENTER_PAGE_URL", configured)
    get_settings.cache_clear()

    builtin = client.get("/api/registries/sources").json()["sources"][0]

    assert builtin["builtin"] is True
    assert builtin["pageUrl"] == expected


@pytest.mark.parametrize("token", ["caf\u00e9", "two words", "smart\u201d"])
def test_tokens_that_cannot_be_sent_in_a_header_are_refused(secret, token):
    form = {**TOKEN_FORM, "token": token}

    for path in ("/api/registries/sources", "/api/registries/sources/test"):
        response = client.post(path, json=form)
        assert response.status_code == 400
        assert token not in response.text


def test_plain_http_needs_confirmation(secret, upstream):
    calls = upstream(base="http://10.1.2.3")
    form = {**TOKEN_FORM, "url": "http://10.1.2.3"}

    assert client.post("/api/registries/sources", json=form).status_code == 400
    assert client.post("/api/registries/sources/test", json=form).status_code == 400
    assert calls == []
    assert client.post("/api/registries/sources", json={**form, "allowInsecureHttp": True}).status_code == 201


def test_plain_http_token_url_needs_confirmation(secret):
    form = {"name": "N", "url": BASE, "authType": "oauth", "tokenUrl": "http://i.example.com/t", "clientId": "c", "clientSecret": "s"}

    assert client.post("/api/registries/sources", json=form).status_code == 400


# --- Store failures ----------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_kubernetes_errors_become_503_and_never_log_the_user_out(secret, status):
    secret.error = ApiException(status=status, reason="k8s says no")

    response = client.post("/api/registries/sources", json=TOKEN_FORM)

    assert response.status_code == 503
    assert "k8s says no" not in response.text


def test_failed_metadata_write_removes_the_new_credentials(secret, monkeypatch):
    def broken(_sources):
        raise registry_store.StoreUnavailable("disk full")

    monkeypatch.setattr(registry_store, "_write_sources", broken)

    assert client.post("/api/registries/sources", json=TOKEN_FORM).status_code == 503
    assert secret.data == {}


def test_orphaned_credentials_are_pruned_on_the_next_change(secret):
    secret.data["orphan"] = base64.b64encode(b"{}").decode()

    add()

    assert "orphan" not in secret.data


def test_missing_credentials_are_reported_without_calling_the_registry(secret, upstream):
    calls = upstream()
    created = add()
    secret.data.clear()

    body = client.get("/api/registries/agents").json()

    assert body["registries"][0]["status"] == "credentials_missing"
    assert calls == []
    assert client.get("/api/registries/sources").json()["sources"][0]["credentialsSet"] is False
    assert client.post(f"/api/registries/sources/{created['id']}/test").json()["classification"] == "credentials_missing"


def test_unavailable_store_does_not_break_the_in_cluster_registry(secret, monkeypatch, upstream):
    monkeypatch.setenv("REGISTRY_CENTER_URL", "http://rc:5000")
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", "tok")
    get_settings.cache_clear()
    add()
    secret.error = ApiException(status=403, reason="forbidden")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_: httpx.Response(200, json={"agentCards": [], "statusCounts": {}}, request=httpx.Request("GET", url)),
    )

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    assert response.json()["storeError"]


# --- Editing -----------------------------------------------------------------


EDIT_OAUTH_FORM = {
    "name": "Idp",
    "url": BASE,
    "authType": "oauth",
    "tokenUrl": "https://iam.example.com/token",
    "clientId": "client",
    "clientSecret": SECRET,
}


def edit(source_id, form):
    return client.put(f"/api/registries/sources/{source_id}", json=form)


def blank_credentials(form):
    return {key: value for key, value in form.items() if key not in ("token", "username", "password", "clientId", "clientSecret", "scope")}


def stored(secret, source_id):
    return json.loads(base64.b64decode(secret.data[source_id]))


def test_edit_changes_details_and_keeps_id_creation_time_and_credentials(secret):
    created = add()
    patches = len(secret.patches)

    response = edit(created["id"], {"name": "Renamed", "url": f"{BASE}/other", "pageUrl": "https://p.example.com", "authType": "token", "skipTlsVerify": True})

    assert response.status_code == 200, response.text
    updated = response.json()
    assert (updated["id"], updated["createdAt"]) == (created["id"], created["createdAt"])
    assert (updated["name"], updated["url"], updated["pageUrl"], updated["skipTlsVerify"]) == ("Renamed", f"{BASE}/other", "https://p.example.com", True)
    assert stored(secret, created["id"]) == {"token": SECRET}
    assert len(secret.patches) == patches, "unchanged credentials must not be rewritten"
    assert SECRET not in response.text


def test_edit_can_clear_the_registry_page(secret):
    created = add({**TOKEN_FORM, "pageUrl": "https://p.example.com"})

    assert edit(created["id"], {**blank_credentials(TOKEN_FORM), "pageUrl": ""}).json()["pageUrl"] is None


def test_edit_replaces_credentials_when_they_are_entered(secret):
    created = add()

    assert edit(created["id"], {**TOKEN_FORM, "token": "new-token"}).status_code == 200

    assert stored(secret, created["id"]) == {"token": "new-token"}


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://evil.example.com"},
        {"url": "http://registry.example.com", "allowInsecureHttp": True},
        {"url": "https://registry.example.com:8443"},
        {"url": "https://registry.example.com@evil.example.com"},
        {"authType": "basic"},
        {"authType": "oauth", "tokenUrl": "https://iam.example.com/token"},
    ],
)
def test_saved_token_never_follows_a_changed_destination_on_save_or_test(secret, upstream, changes):
    created = add()
    calls = upstream()
    form = {**blank_credentials(TOKEN_FORM), **changes}

    assert edit(created["id"], form).status_code == 400
    assert client.post(f"/api/registries/sources/{created['id']}/test", json=form).status_code == 400
    assert calls == []
    assert client.get("/api/registries/sources").json()["sources"][0]["url"] == BASE
    assert stored(secret, created["id"]) == {"token": SECRET}


@pytest.mark.parametrize(
    "changes",
    [
        {"tokenUrl": "https://evil.example.com/token"},
        {"url": "https://evil.example.com"},
        {"grantType": "password"},
    ],
)
def test_saved_oauth_secret_never_follows_a_changed_destination(secret, upstream, changes):
    created = add(EDIT_OAUTH_FORM)
    calls = upstream()
    form = {**blank_credentials(EDIT_OAUTH_FORM), **changes}

    assert edit(created["id"], form).status_code == 400
    assert client.post(f"/api/registries/sources/{created['id']}/test", json=form).status_code == 400
    assert calls == []


def test_oauth_edit_with_the_same_destination_keeps_the_secret(secret):
    created = add({**EDIT_OAUTH_FORM, "scope": "read"})

    response = edit(created["id"], {**blank_credentials(EDIT_OAUTH_FORM), "name": "Renamed", "tokenUrl": "https://iam.example.com/v2/token", "grantType": "client_credentials"})

    assert response.status_code == 200, response.text
    assert stored(secret, created["id"]) == {"clientId": "client", "clientSecret": SECRET, "scope": "read"}


def test_partly_entered_credentials_do_not_mix_with_saved_ones(secret):
    created = add({"name": "B", "url": BASE, "authType": "basic", "username": "u", "password": SECRET})

    response = edit(created["id"], {"name": "B", "url": BASE, "authType": "basic", "username": "other"})

    assert response.status_code == 400
    assert stored(secret, created["id"]) == {"username": "u", "password": SECRET}


def test_changing_to_no_credentials_deletes_the_saved_ones(secret):
    created = add()

    assert edit(created["id"], {"name": "Partner", "url": "http://elsewhere.example.com", "authType": "none"}).status_code == 200

    assert stored(secret, created["id"]) == {}


def test_edit_can_restore_missing_credentials(secret):
    created = add()
    del secret.data[created["id"]]
    form = blank_credentials(TOKEN_FORM)

    assert edit(created["id"], form).status_code == 400
    assert edit(created["id"], TOKEN_FORM).status_code == 200
    assert stored(secret, created["id"]) == {"token": SECRET}


def test_edit_refuses_another_registrys_name_but_allows_its_own(secret):
    created = add()
    add({**TOKEN_FORM, "name": "Other"})
    form = blank_credentials(TOKEN_FORM)

    assert edit(created["id"], {**form, "name": "other"}).status_code == 409
    assert edit(created["id"], {**form, "name": "PARTNER"}).status_code == 200


@pytest.mark.parametrize("source_id, status", [("in-cluster", 400), ("missing", 404)])
def test_edit_refuses_the_in_cluster_and_unknown_registries(secret, source_id, status):
    assert edit(source_id, TOKEN_FORM).status_code == status


def test_non_admins_cannot_edit_or_test_edits(secret, upstream):
    created = add()
    calls = upstream()
    act_as(VIEWER)

    assert edit(created["id"], {**TOKEN_FORM, "name": "Hacked"}).status_code == 403
    assert client.post(f"/api/registries/sources/{created['id']}/test", json=TOKEN_FORM).status_code == 403
    assert calls == []


def test_failed_metadata_write_during_edit_restores_the_old_credentials(secret, monkeypatch):
    created = add()

    def fail(_):
        raise registry_store.StoreUnavailable("disk full")

    monkeypatch.setattr(registry_store, "_write_sources", fail)

    assert edit(created["id"], {**TOKEN_FORM, "token": "new-token"}).status_code == 503
    assert stored(secret, created["id"]) == {"token": SECRET}


def test_edit_with_new_credentials_lifts_a_lockout_but_the_same_ones_do_not(secret, upstream):
    created = add()
    calls = upstream({f"{API}/agent-cards/manage": (401, {})})
    client.get("/api/registries/agents")
    made = len(calls)

    assert edit(created["id"], {**blank_credentials(TOKEN_FORM), "name": "Renamed"}).status_code == 200
    client.get("/api/registries/agents?refresh=true")
    assert len(calls) == made, "the same credentials must stay locked out"

    assert edit(created["id"], {**TOKEN_FORM, "name": "Renamed", "token": "new-token"}).status_code == 200
    client.get("/api/registries/agents?refresh=true")
    assert len(calls) > made


def test_testing_edits_uses_the_form_values_without_saving(secret, upstream):
    created = add()
    calls = upstream(base=f"{BASE}/other")
    form = {**blank_credentials(TOKEN_FORM), "url": f"{BASE}/other"}

    result = client.post(f"/api/registries/sources/{created['id']}/test", json=form).json()

    assert result["ok"] is True
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert client.get("/api/registries/sources").json()["sources"][0]["url"] == BASE


def test_testing_unchanged_edits_respects_the_saved_registrys_lockout(secret, upstream):
    created = add()
    upstream({f"{API}/agent-cards/manage": (401, {})})
    client.get("/api/registries/agents")
    calls = upstream()

    result = client.post(f"/api/registries/sources/{created['id']}/test", json=blank_credentials(TOKEN_FORM)).json()

    assert result["classification"] == "cooldown"
    assert calls == []


# --- Connection test ---------------------------------------------------------


def test_successful_test_reports_admin_level_and_agent_count(secret, upstream):
    upstream()

    body = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()

    assert body["ok"] is True
    assert body["level"] == "admin"
    assert body["agentCount"] == 1


def test_test_with_a_discovery_only_token_warns(secret, upstream):
    upstream({f"{API}/agent-cards/manage": (403, {})})

    body = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()

    assert body["ok"] is True
    assert body["level"] == "discovery"
    assert body["warnings"]


@pytest.mark.parametrize(
    "status, payload, classification",
    [
        (401, {}, "bad_credentials"),
        (403, {}, "forbidden"),
        (404, {}, "not_registry"),
        (429, {}, "rate_limited"),
        (503, {}, "auth_unavailable"),
        (500, {}, "upstream_error"),
        (302, {}, "upstream_error"),
        (200, ["not", "object"], "not_registry"),
        (200, {"other": 1}, "not_registry"),
    ],
)
def test_test_classifies_the_discovery_response(secret, upstream, status, payload, classification):
    upstream({f"{API}/agent-cards": (status, payload)})

    response = client.post("/api/registries/sources/test", json=TOKEN_FORM)

    assert response.status_code == 200
    assert response.json()["classification"] == classification
    assert response.json()["ok"] is False


@pytest.mark.parametrize(
    "error, classification",
    [
        (outbound.BlockedAddress("h"), "blocked"),
        (outbound.ResolveFailed("h"), "dns_error"),
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ConnectError("refused"), "unreachable"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "tls"),
    ],
)
def test_transport_failures_stop_before_credentials_are_sent(secret, upstream, error, classification):
    calls = upstream({"/health": error})

    body = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()

    assert body["classification"] == classification
    assert [call["path"] for call in calls] == ["/health"]
    assert calls[0]["headers"] is None


@pytest.mark.parametrize(
    "error",
    [UnicodeEncodeError("ascii", "x", 0, 1, "bad"), httpx.DecodingError("bad gzip"), outbound.UnusableResponse("h")],
)
def test_unexpected_send_errors_become_a_failure_not_a_500(secret, upstream, error):
    upstream({f"{API}/agent-cards": error})

    response = client.post("/api/registries/sources/test", json=TOKEN_FORM)

    assert response.status_code == 200
    assert response.json()["classification"] == "upstream_error"


def test_json_nested_too_deeply_to_parse_is_not_a_500(secret, upstream, monkeypatch):
    upstream()
    deep = "[" * 200_000

    def send(method, url, **_):
        return httpx.Response(200, content=deep.encode(), request=httpx.Request(method, url))

    monkeypatch.setattr(outbound, "send", send)
    add()

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    assert response.json()["registries"][0]["status"] == "error"


def test_passed_through_json_too_deep_to_serialize_is_dropped(secret, upstream):
    deep = current = {}
    for _ in range(500):
        current["x"] = {}
        current = current["x"]
    card = {
        **MANAGED,
        "securitySchemes": deep,
        "capabilities": {"extensions": [{"uri": "u", "params": deep}, {"uri": "v", "params": {"ok": {"a": 1}}}]},
    }
    upstream({f"{API}/agent-cards/manage": (200, {"agentCards": [card]})})
    add()

    agent = client.get("/api/registries/agents").json()["agents"][0]

    assert agent["securitySchemes"] == {}
    assert "params" not in agent["capabilities"]["extensions"][0]
    assert agent["capabilities"]["extensions"][1]["params"] == {"ok": {"a": 1}}


@pytest.mark.parametrize("page", [b"<!DOCTYPE html><html></html>", b"\n  <HTML><body></body></HTML>"])
def test_web_page_response_suggests_the_portal_registry_path(secret, upstream, monkeypatch, page):
    upstream()

    def send(method, url, **_):
        return httpx.Response(200, content=page, request=httpx.Request(method, url))

    monkeypatch.setattr(outbound, "send", send)
    form = {**TOKEN_FORM, "url": "https://portal.example.com:9003/registry"}

    body = client.post("/api/registries/sources/test", json=form).json()

    assert body["classification"] == "web_page"
    assert body["warnings"] == ["If this is an OpenAN Portal, its registry API is usually at https://portal.example.com:9003/api/registry-center."]


def test_listing_a_web_page_reports_it_as_a_warning(secret, upstream, monkeypatch):
    upstream()
    add()

    def send(method, url, **_):
        return httpx.Response(200, content=b"<!doctype html>", request=httpx.Request(method, url))

    monkeypatch.setattr(outbound, "send", send)

    entry = client.get("/api/registries/agents").json()["registries"][0]

    assert entry["status"] == "error"
    assert entry["message"] == "The URL returned a web page instead of the registry API."


def test_unrouted_health_route_does_not_fail_the_test(secret, upstream):
    upstream({"/health": (404, {})})

    assert client.post("/api/registries/sources/test", json=TOKEN_FORM).json()["ok"] is True


def test_rejected_credentials_are_tried_once_then_cool_down(secret, upstream):
    calls = upstream({f"{API}/agent-cards": (401, {})})

    first = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()
    calls.clear()
    second = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()
    changed = client.post("/api/registries/sources/test", json={**TOKEN_FORM, "token": "other"}).json()

    assert first["classification"] == "bad_credentials"
    assert any("banned" in warning for warning in first["warnings"])
    assert second["classification"] == "cooldown"
    assert changed["classification"] == "bad_credentials"
    assert len([call for call in calls if call["path"] == f"{API}/agent-cards"]) == 1


def test_a_listing_rejection_stops_the_saved_test_from_calling_again(secret, upstream):
    calls = upstream({f"{API}/agent-cards/manage": (401, {})})
    created = add()
    client.get("/api/registries/agents")
    calls.clear()

    body = client.post(f"/api/registries/sources/{created['id']}/test").json()

    assert body["classification"] == "cooldown"
    assert calls == []


def test_a_saved_test_rejection_stops_the_listing_from_calling_again(secret, upstream):
    calls = upstream({f"{API}/agent-cards": (401, {})})
    created = add()
    assert client.post(f"/api/registries/sources/{created['id']}/test").json()["classification"] == "bad_credentials"
    calls.clear()

    body = client.get("/api/registries/agents?refresh=true").json()

    assert body["registries"][0]["message"] == "The registry rejected the credentials."
    assert calls == []


@pytest.mark.parametrize(
    "status, classification", [(401, "bad_credentials"), (429, "rate_limited"), (503, "auth_unavailable")]
)
def test_manage_failures_that_affect_the_credentials_fail_the_test(secret, upstream, status, classification):
    upstream({f"{API}/agent-cards/manage": (status, {})})

    body = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()

    assert body["ok"] is False
    assert body["classification"] == classification


@pytest.mark.parametrize("result", [(500, {}), httpx.ReadTimeout("t")])
def test_other_manage_failures_warn_without_claiming_a_role_problem(secret, upstream, result):
    upstream({f"{API}/agent-cards/manage": result})

    body = client.post("/api/registries/sources/test", json=TOKEN_FORM).json()

    assert body["ok"] is True
    assert body["level"] is None
    assert body["warnings"] == ["Could not check whether these credentials can read all review states."]


def test_basic_rejection_explains_registry_center_needs_a_bearer_token(secret, upstream):
    calls = upstream({f"{API}/agent-cards": (401, {})})
    form = {"name": "B", "url": BASE, "authType": "basic", "username": "u", "password": "p"}

    body = client.post("/api/registries/sources/test", json=form).json()

    assert any("Bearer" in warning for warning in body["warnings"])
    discovery = next(call for call in calls if call["path"] == f"{API}/agent-cards")
    assert discovery["headers"]["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()


def test_skip_tls_is_passed_to_every_call(secret, upstream):
    calls = upstream()

    client.post("/api/registries/sources/test", json={**TOKEN_FORM, "skipTlsVerify": True})

    assert calls and all(call["verify"] is False for call in calls)


def test_test_endpoint_is_rate_limited(secret, upstream):
    upstream()

    codes = [client.post("/api/registries/sources/test", json=TOKEN_FORM).status_code for _ in range(11)]

    assert codes[:10] == [200] * 10
    assert codes[10] == 429


# --- No credentials ----------------------------------------------------------

NO_AUTH_FORM = {"name": "Open", "url": BASE, "authType": "none"}


def test_no_credentials_sends_no_authorization_header(secret, upstream):
    calls = upstream()
    created = add(NO_AUTH_FORM)

    assert client.post("/api/registries/sources/test", json=NO_AUTH_FORM).json()["ok"] is True
    body = client.get("/api/registries/agents").json()

    assert created["authType"] == "none"
    assert created["credentialsSet"] is True
    assert body["registries"][0]["status"] == "ok"
    assert calls and all("Authorization" not in (call["headers"] or {}) for call in calls)


def test_no_credentials_ignores_any_submitted_credentials(secret, upstream):
    calls = upstream()

    created = add({**NO_AUTH_FORM, "token": SECRET, "username": "u", "password": SECRET})
    client.get("/api/registries/agents")

    assert SECRET not in json.dumps(secret.patches)
    assert json.loads(base64.b64decode(secret.data[created["id"]])) == {}
    assert all("Authorization" not in (call["headers"] or {}) for call in calls)


def test_no_credentials_over_http_needs_no_confirmation(secret, upstream):
    upstream(base="http://10.1.2.3")

    assert client.post("/api/registries/sources", json={**NO_AUTH_FORM, "url": "http://10.1.2.3"}).status_code == 201


def test_no_credentials_rejected_says_credentials_are_required(secret, upstream):
    upstream({f"{API}/agent-cards": (401, {})})

    body = client.post("/api/registries/sources/test", json=NO_AUTH_FORM).json()

    assert body["classification"] == "bad_credentials"
    assert "This registry requires credentials." in body["warnings"]


# --- OAuth -------------------------------------------------------------------

TOKEN_URL = "https://iam.example.com/token"
OAUTH_FORM = {"name": "O", "url": BASE, "authType": "oauth", "tokenUrl": TOKEN_URL, "clientId": "id", "clientSecret": "sec", "scope": "registry.read"}
PASSWORD_FORM = {"name": "P", "url": BASE, "authType": "oauth", "grantType": "password", "tokenUrl": TOKEN_URL, "username": "u", "password": "pw"}


@pytest.mark.parametrize(
    "form, expected",
    [
        (OAUTH_FORM, {"grant_type": "client_credentials", "client_id": "id", "client_secret": "sec", "scope": "registry.read"}),
        (PASSWORD_FORM, {"grant_type": "password", "username": "u", "password": "pw"}),
    ],
)
def test_oauth_exchanges_each_grant_for_a_bearer_token(secret, upstream, form, expected):
    calls = upstream({TOKEN_URL: (200, {"access_token": "issued", "expires_in": 300})})

    assert client.post("/api/registries/sources/test", json=form).json()["ok"] is True

    exchange = next(call for call in calls if call["path"] == TOKEN_URL)
    assert exchange["method"] == "POST"
    assert exchange["data"] == expected
    assert next(call for call in calls if call["path"] == f"{API}/agent-cards")["headers"]["Authorization"] == "Bearer issued"
    assert registries._oauth_tokens == {}


@pytest.mark.parametrize("result", [(401, {}), (200, {}), (200, {"access_token": 5})])
def test_failed_token_exchange_is_classified_and_never_reaches_the_registry(secret, upstream, result):
    calls = upstream({TOKEN_URL: result})

    body = client.post("/api/registries/sources/test", json=OAUTH_FORM).json()

    assert body["classification"] == "token_endpoint_failed"
    assert not [call for call in calls if API in call["path"]]


def test_saved_oauth_registry_reuses_its_token(secret, upstream):
    calls = upstream({TOKEN_URL: (200, {"access_token": "issued", "expires_in": 300})})
    add(OAUTH_FORM)

    client.get("/api/registries/agents?refresh=true")
    client.get("/api/registries/agents?refresh=true")

    assert len([call for call in calls if call["path"] == TOKEN_URL]) == 1


def test_a_rejected_oauth_token_is_not_reused(secret, upstream):
    calls = upstream(
        {TOKEN_URL: (200, {"access_token": "issued", "expires_in": 300}), f"{API}/agent-cards/manage": (401, {})}
    )
    add(OAUTH_FORM)

    client.get("/api/registries/agents")
    registries._failures.clear()
    client.get("/api/registries/agents")

    assert len([call for call in calls if call["path"] == TOKEN_URL]) == 2


@pytest.mark.parametrize("expires_in, cached", [(10**12, True), (float("inf"), True), ("soon", True), (10, False)])
def test_token_lifetime_from_the_token_url_is_capped(secret, upstream, expires_in, cached, monkeypatch):
    def send(method, url, headers=None, params=None, data=None, verify=True):
        if url == TOKEN_URL:
            content = json.dumps({"access_token": "issued", "expires_in": expires_in}).encode()
            return httpx.Response(200, content=content, request=httpx.Request(method, url))
        return httpx.Response(200, json={"agentCards": [], "agents": []}, request=httpx.Request(method, url))

    monkeypatch.setattr(outbound, "send", send)
    add(OAUTH_FORM)
    # "soon" has no usable lifetime, so it falls back to 60 seconds.
    client.get("/api/registries/agents")

    if not cached:
        assert registries._oauth_tokens == {}
        return
    expiry = next(iter(registries._oauth_tokens.values()))[0]
    assert expiry - time.monotonic() <= registries.MAX_OAUTH_TOKEN_SECONDS


# --- Listing -----------------------------------------------------------------


def test_listing_tags_agents_with_their_registry(secret, upstream):
    upstream()
    created = add()

    body = client.get("/api/registries/agents").json()

    assert body["agents"][0]["registry"] == {"id": created["id"], "name": "Partner", "builtin": False}
    assert body["registries"][0]["status"] == "ok"
    assert body["registries"][0]["agentCount"] == 1


def test_discovery_only_token_falls_back_and_hides_review(secret, upstream):
    calls = upstream({f"{API}/agent-cards/manage": (403, {})})
    created = add()

    body = client.get("/api/registries/agents").json()
    detail = client.get(f"/api/registries/agents/{created['id']}/org/Remote").json()

    assert body["agents"][0]["publishStatus"] == "published"
    assert body["fullAccess"] is True
    assert body["registries"][0]["fullAccess"] is False
    assert detail["fullAccess"] is False
    assert detail["review"] is None
    assert not [call for call in calls if "review-status" in call["path"] or "agents/health" in call["path"]]


def test_a_failing_registry_becomes_a_warning_next_to_a_working_one(secret, upstream, monkeypatch):
    monkeypatch.setenv("REGISTRY_CENTER_URL", "http://rc:5000")
    monkeypatch.setenv("REGISTRY_CENTER_TOKEN", "tok")
    get_settings.cache_clear()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_: httpx.Response(
            200,
            json={"agentCards": [{"name": "Local", "provider": {"organization": "org"}}], "agents": []},
            request=httpx.Request("GET", url),
        ),
    )
    upstream({f"{API}/agent-cards/manage": httpx.ConnectError("refused")})
    add()

    response = client.get("/api/registries/agents")

    body = response.json()
    assert response.status_code == 200
    assert [agent["name"] for agent in body["agents"]] == ["Local"]
    assert body["agents"][0]["registry"]["builtin"] is True
    assert {entry["id"]: entry["status"] for entry in body["registries"]}["in-cluster"] == "ok"
    failed = next(entry for entry in body["registries"] if entry["id"] != "in-cluster")
    assert failed["status"] == "error"
    assert failed["message"] == "The registry could not be reached."


def test_only_external_registries_failing_still_returns_200(secret, upstream):
    upstream({f"{API}/agent-cards/manage": httpx.ConnectError("refused")})
    add()

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    assert response.json()["agents"] == []


@pytest.mark.parametrize(
    "manage",
    [pytest.param(None, id="results"), pytest.param(httpx.ConnectError("refused"), id="outage")],
)
def test_responses_are_cached_and_refresh_bypasses_the_cache(secret, upstream, manage):
    calls = upstream({f"{API}/agent-cards/manage": manage} if manage else None)
    add()
    count = lambda: len([call for call in calls if call["path"] == f"{API}/agent-cards/manage"])

    client.get("/api/registries/agents")
    client.get("/api/registries/agents")
    assert count() == 1

    client.get("/api/registries/agents?refresh=true")
    assert count() == 2


def test_non_admins_cannot_bypass_the_cache(secret, upstream):
    calls = upstream()
    add()
    act_as(VIEWER)

    for _ in range(3):
        client.get("/api/registries/agents?refresh=true")

    assert len(calls) == 1


def test_concurrent_requests_share_one_upstream_call(secret, upstream, monkeypatch):
    calls = upstream()
    add()
    original = outbound.send
    release = threading.Event()

    def slow(method, url, **kwargs):
        release.wait(5)
        return original(method, url, **kwargs)

    monkeypatch.setattr(outbound, "send", slow)
    source = registries._external_sources()[0]
    futures = [registries._shared_task((source.id, True), registries._fetch_external, source, True) for _ in range(5)]

    assert len(set(futures)) == 1
    release.set()
    futures[0].result(timeout=5)
    assert len([call for call in calls if call["path"] == f"{API}/agent-cards/manage"]) == 1


@pytest.mark.parametrize("status", [401, 429])
def test_refresh_cannot_drive_a_rejected_credential_into_a_ban(secret, upstream, status):
    calls = upstream({f"{API}/agent-cards/manage": (status, {})})
    add()

    for _ in range(3):
        client.get("/api/registries/agents?refresh=true")

    assert len(calls) == 1


def test_successful_saved_test_clears_a_recorded_outage(secret, upstream):
    upstream({f"{API}/agent-cards/manage": httpx.ConnectError("refused")})
    created = add()
    assert client.get("/api/registries/agents").json()["registries"][0]["status"] == "error"

    upstream()

    assert client.post(f"/api/registries/sources/{created['id']}/test").json()["ok"] is True
    assert client.get("/api/registries/agents").json()["registries"][0]["status"] == "ok"


def test_a_hung_registry_does_not_hold_up_the_page(secret, upstream, monkeypatch):
    upstream()
    add()
    add({**TOKEN_FORM, "name": "Hung", "url": "https://hung.example.com"})
    release = threading.Event()
    original = outbound.send

    def hanging(method, url, **kwargs):
        if url.startswith("https://hung.example.com"):
            release.wait(5)
            raise httpx.ReadTimeout("t")
        return original(method, url, **kwargs)

    monkeypatch.setattr(outbound, "send", hanging)
    monkeypatch.setattr(registries, "FAN_OUT_DEADLINE_SECONDS", 0.5)

    started = time.monotonic()
    body = client.get("/api/registries/agents").json()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 3
    assert [agent["name"] for agent in body["agents"]] == ["Remote"]
    assert {entry["name"]: entry["status"] for entry in body["registries"]} == {"Partner": "ok", "Hung": "error"}


def test_malformed_cards_are_cleaned_so_they_cannot_break_the_page(secret, upstream):
    upstream(
        {
            f"{API}/agent-cards/manage": (
                200,
                {
                    "agentCards": [
                        "not a card",
                        {"name": 5, "provider": {"organization": "org"}},
                        {"name": "No org"},
                        {
                            "name": "Odd",
                            "provider": {"organization": "org", "url": {"x": 1}},
                            "description": {"x": 1},
                            "publishStatus": ["published"],
                            "skills": ["bad", {"name": {"x": 1}, "tags": "bad"}, {"name": "Good", "tags": ["t", 1]}],
                            "supportedInterfaces": "bad",
                            "capabilities": {"streaming": "yes", "extensions": [1, {"uri": 2}]},
                            "defaultInputModes": [1, "text"],
                        },
                    ],
                    "statusCounts": ["bad"],
                },
            ),
            f"{API}/agents/health": (200, {"agents": ["bad", {"organization": ["x"], "name": {"y": 1}}], "config": "bad"}),
        }
    )
    add()

    response = client.get("/api/registries/agents")

    assert response.status_code == 200
    agents = response.json()["agents"]
    assert [agent["name"] for agent in agents] == ["Odd"]
    odd = agents[0]
    assert "description" not in odd and "publishStatus" not in odd and "url" not in odd["provider"]
    assert [skill.get("name") for skill in odd["skills"]] == [None, "Good"]
    assert odd["skills"][1]["tags"] == ["t"]
    assert odd["supportedInterfaces"] == []
    assert odd["capabilities"]["streaming"] is False
    assert odd["defaultInputModes"] == ["text"]


# --- Detail ------------------------------------------------------------------


def test_detail_of_an_external_agent(secret, upstream):
    upstream(
        {
            f"{API}/agent-cards-with-layer/org/Remote": (200, {"layer": "service"}),
            f"{API}/agent-cards/org/Remote/review-status": (
                200,
                {"status": "registered", "history": [{"to_status": "registered", "at": "t", "reason": {"x": 1}}]},
            ),
        }
    )
    created = add()

    body = client.get(f"/api/registries/agents/{created['id']}/org/Remote").json()

    assert body["registry"]["id"] == created["id"]
    assert body["publishStatus"] == "registered"
    assert body["layer"] == "service"
    assert body["review"]["history"] == [{"to_status": "registered", "at": "t"}]
    assert "registry" not in body["card"]


def test_detail_of_a_hung_registry_does_not_hold_the_request(secret, upstream, monkeypatch):
    upstream()
    created = add()
    release = threading.Event()

    def hanging(method, url, **_):
        release.wait(5)
        raise httpx.ReadTimeout("t")

    monkeypatch.setattr(outbound, "send", hanging)
    monkeypatch.setattr(registries, "FAN_OUT_DEADLINE_SECONDS", 0.3)

    started = time.monotonic()
    response = client.get(f"/api/registries/agents/{created['id']}/org/Remote")
    elapsed = time.monotonic() - started
    release.set()

    assert response.status_code == 502
    assert elapsed < 3


def test_detail_is_cached_per_role(secret, upstream):
    calls = upstream()
    created = add()
    path = f"/api/registries/agents/{created['id']}/org/Remote"
    layer = lambda: len([call for call in calls if "with-layer" in call["path"]])

    assert client.get(path).json()["fullAccess"] is True
    client.get(path)
    assert layer() == 1

    act_as(VIEWER)
    assert client.get(path).json()["fullAccess"] is False
    assert layer() == 2


def test_same_agent_in_two_registries_stays_distinct(secret, upstream, monkeypatch):
    upstream()
    first = add()
    second = add({**TOKEN_FORM, "name": "Other", "url": "https://other.example.com"})
    original = outbound.send

    def by_host(method, url, **kwargs):
        if url.startswith("https://other.example.com"):
            card = {**MANAGED, "description": "other"}
            return httpx.Response(200, json={"agentCards": [card]}, request=httpx.Request(method, url))
        return original(method, url, **kwargs)

    monkeypatch.setattr(outbound, "send", by_host)

    assert "description" not in client.get(f"/api/registries/agents/{first['id']}/org/Remote").json()["card"]
    assert client.get(f"/api/registries/agents/{second['id']}/org/Remote").json()["card"]["description"] == "other"


@pytest.mark.parametrize("path", ["nope/org/Remote", "{id}/org/Nope", "{id}/%2e%2e/Remote"])
def test_unknown_external_agent_is_404(secret, upstream, path):
    upstream()
    created = add()

    assert client.get("/api/registries/agents/" + path.format(id=created["id"])).status_code == 404


# --- Network guard -----------------------------------------------------------


@pytest.fixture
def transport(monkeypatch):
    """Capture what the pinned client sends, without any network."""

    def install(handler):
        monkeypatch.setattr(
            outbound,
            "_client",
            lambda verify: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        )

    return install


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, **_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
    )


class RawStream(httpx.SyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_connection_is_pinned_to_the_checked_address(monkeypatch, transport):
    resolves_to(monkeypatch, "203.0.113.7")
    seen = {}

    def handler(request):
        seen.update(
            url=str(request.url),
            host=request.headers["Host"],
            sni=request.extensions.get("sni_hostname"),
            encoding=request.headers["Accept-Encoding"],
        )
        return httpx.Response(200, stream=RawStream(b'{"ok":', b" true}"))

    transport(handler)

    response = outbound.send("GET", "https://registry.example.com:8443/x")

    assert response.json() == {"ok": True}
    assert seen == {
        "url": "https://203.0.113.7:8443/x",
        "host": "registry.example.com:8443",
        "sni": "registry.example.com",
        "encoding": "identity",
    }


def test_redirects_are_not_followed(monkeypatch, transport):
    resolves_to(monkeypatch, "203.0.113.7")
    transport(lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}, stream=RawStream()))

    assert outbound.send("GET", "https://registry.example.com/").status_code == 302


@pytest.mark.parametrize(
    "addresses", [("127.0.0.1",), ("169.254.169.254",), ("::1",), ("::ffff:127.0.0.1",), ("203.0.113.7", "127.0.0.1"), ("0.0.0.0",)]
)
def test_names_resolving_to_blocked_addresses_are_refused(monkeypatch, transport, addresses):
    resolves_to(monkeypatch, *addresses)
    transport(lambda request: pytest.fail("must not connect"))

    with pytest.raises(outbound.BlockedAddress):
        outbound.send("GET", "https://registry.example.com/")


@pytest.mark.parametrize("address", ["10.1.2.3", "172.16.0.1", "192.168.1.1"])
def test_private_ranges_stay_allowed(monkeypatch, transport, address):
    resolves_to(monkeypatch, address)
    transport(lambda request: httpx.Response(200, stream=RawStream()))

    assert outbound.send("GET", "https://registry.example.com/").status_code == 200


def test_oversized_responses_are_refused(monkeypatch, transport):
    resolves_to(monkeypatch, "203.0.113.7")
    monkeypatch.setattr(outbound, "MAX_RESPONSE_BYTES", 10)
    transport(lambda request: httpx.Response(200, stream=RawStream(b"x" * 6, b"x" * 6)))

    with pytest.raises(outbound.UnusableResponse):
        outbound.send("GET", "https://registry.example.com/")


def test_compressed_responses_are_refused_without_being_decoded(monkeypatch, transport):
    resolves_to(monkeypatch, "203.0.113.7")
    bomb = gzip.compress(b"x" * 1024 * 1024)
    transport(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=RawStream(bomb)))

    with pytest.raises(outbound.UnusableResponse):
        outbound.send("GET", "https://registry.example.com/")


def test_a_server_trickling_its_response_hits_the_overall_deadline(monkeypatch):
    """Uses a real socket, because the per-read timeout never fires against a trickle."""
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def trickle():
        connection, _ = server.accept()
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 OK\r\n")
        try:
            while not stop.is_set():
                connection.sendall(b"X-Slow: 1\r\n")
                time.sleep(0.05)
        except OSError:
            pass
        connection.close()

    threading.Thread(target=trickle, daemon=True).start()
    monkeypatch.setattr(outbound, "_resolve", lambda host, port: "127.0.0.1")
    monkeypatch.setattr(outbound, "TOTAL_SECONDS", 0.3)

    started = time.monotonic()
    try:
        with pytest.raises(httpx.TimeoutException):
            outbound.send("GET", f"http://registry.example.com:{server.getsockname()[1]}/")
    finally:
        stop.set()
        server.close()

    assert time.monotonic() - started < 3


def test_stalled_dns_lookups_do_not_hold_up_other_hosts(monkeypatch):
    release = threading.Event()

    def getaddrinfo(host, port, **_):
        if host.startswith("dead"):
            release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(outbound, "RESOLVE_SECONDS", 0.2)
    stalled = [threading.Thread(target=lambda: pytest.raises(outbound.ResolveFailed, outbound._resolve, "dead.example.com", 443)) for _ in range(20)]
    for thread in stalled:
        thread.start()

    try:
        assert outbound._resolve("healthy.example.com", 443) == "203.0.113.7"
    finally:
        release.set()
        for thread in stalled:
            thread.join()


def test_a_stalled_dns_lookup_is_abandoned(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: release.wait(5))
    monkeypatch.setattr(outbound, "RESOLVE_SECONDS", 0.1)

    try:
        with pytest.raises(outbound.ResolveFailed):
            outbound.send("GET", "https://registry.example.com/")
    finally:
        release.set()
