"""Only Keycloak refusing a refresh token may end a session; temporary failures must not."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import get_settings
from routers import auth

COOKIE = get_settings().AUTH_REFRESH_COOKIE_NAME

app = FastAPI()
app.include_router(auth.router)


@pytest.fixture(autouse=True)
def keycloak(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", "http://keycloak.example.com/auth")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def post_refresh(monkeypatch, handler, cookie="saved-refresh-token"):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app, base_url="https://testserver")
    if cookie:
        client.cookies.set(COOKIE, cookie, path="/auth")
    return client.post(f"{auth.router.prefix}/refresh")


def clears_cookie(response):
    return any(COOKIE in value and "Max-Age=0" in value for value in response.headers.get_list("set-cookie"))


@pytest.mark.parametrize("status", [400, 401])
def test_refused_refresh_token_ends_the_session(monkeypatch, status):
    response = post_refresh(monkeypatch, lambda request: httpx.Response(status, json={"error": "invalid_grant"}))

    assert response.status_code == 401
    assert clears_cookie(response)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_keycloak_errors_keep_the_session(monkeypatch, status):
    response = post_refresh(monkeypatch, lambda request: httpx.Response(status))

    assert response.status_code == 503
    assert not clears_cookie(response)


def test_unreachable_keycloak_keeps_the_session(monkeypatch):
    def unreachable(request):
        raise httpx.ConnectError("down", request=request)

    response = post_refresh(monkeypatch, unreachable)

    assert response.status_code == 503
    assert not clears_cookie(response)


def test_refresh_returns_the_new_token(monkeypatch):
    tokens = {"access_token": "new", "expires_in": 1800, "refresh_token": "next", "refresh_expires_in": 1800}

    response = post_refresh(monkeypatch, lambda request: httpx.Response(200, json=tokens))

    assert response.status_code == 200
    assert response.json()["access_token"] == "new"


def test_no_cookie_means_no_session(monkeypatch):
    assert post_refresh(monkeypatch, lambda request: httpx.Response(200), cookie=None).status_code == 401
