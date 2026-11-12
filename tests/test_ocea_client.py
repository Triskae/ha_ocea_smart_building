"""Tests for Ocea authentication and HTTP contracts using a local server."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from custom_components.ocea_smart_building.const import UA
from custom_components.ocea_smart_building.ocea import (
    OceaApiClient,
    OceaApiError,
    OceaAuthError,
)

pytestmark = pytest.mark.usefixtures("local_http_only")


async def _server(aiohttp_server, monkeypatch, state):
    """Provide an isolated, synthetic B2C and Ocea API endpoint."""
    async def authorize(request):
        state["authorize"] = dict(request.query)
        response = web.Response(text=state.get("html", '{"transId":"StateProperties=abc=="}'))
        if not state.get("no_cookie"):
            cookies = state.get("cookies")
            response.set_cookie(
                "x-ms-cpim-csrf",
                cookies.pop(0) if cookies else "csrf-placeholder",
                path=state.get("cookie_path", "/"),
            )
        return response

    async def login(request):
        state["login_path"] = request.raw_path
        state["login_cookie"] = request.cookies.get("x-ms-cpim-csrf")
        state.setdefault("login_cookies", []).append(state["login_cookie"])
        state["csrf"] = request.headers.get("X-CSRF-TOKEN")
        state["user_agent"] = request.headers.get("User-Agent")
        state["login_form"] = dict(await request.post())
        return web.Response(status=state.get("login_status", 200), text=state.get("login_body", '{"status":"200"}'))

    async def confirmed(request):
        state["confirmed_path"] = request.raw_path
        location = state.get("location", "http://example.test/#code=code-placeholder")
        return web.Response(status=302, headers={"Location": location} if location else {})

    async def token(request):
        form = dict(await request.post())
        state.setdefault("token_forms", []).append(form)
        if form["grant_type"] == "refresh_token":
            return web.json_response(
                state.get("refresh_response", {"access_token": "new-placeholder"}),
                status=state.get("refresh_status", 200),
            )
        return web.json_response(
            state.get("token_response", {"access_token": "token-placeholder", "refresh_token": "refresh-placeholder"}),
            status=state.get("token_status", 200),
        )

    async def api(request):
        state.setdefault("requests", []).append(
            (request.method, request.path, request.headers.get("Authorization"))
        )
        if request.method == "POST":
            state["payload"] = await request.json()
        statuses = state.get("api_statuses", [])
        status = statuses.pop(0) if statuses else 200
        return web.json_response(state.get("api_response", {"resident": {}, "occupations": []}), status=status)

    app = web.Application()
    app.router.add_get("/authorize", authorize)
    app.router.add_post("/tenant.onmicrosoft.com/B2C_1A_SIGNUP_SIGNIN/SelfAsserted", login)
    app.router.add_get("/tenant.onmicrosoft.com/B2C_1A_SIGNUP_SIGNIN/api/CombinedSigninAndSignup/confirmed", confirmed)
    app.router.add_post("/token", token)
    app.router.add_route("*", "/api/{path:.*}", api)
    server = await aiohttp_server(app)
    base = str(server.make_url("/")).rstrip("/")
    for name, value in {
        "B2C_BASE": base,
        "B2C_TENANT": "tenant",
        "B2C_AUTHORIZE": f"{base}/authorize",
        "B2C_TOKEN": f"{base}/token",
    }.items():
        monkeypatch.setattr(f"custom_components.ocea_smart_building.ocea.auth.{name}", value)
    monkeypatch.setattr("custom_components.ocea_smart_building.ocea.client.API_BASE", base)


def _client(session):
    return OceaApiClient(
        "resident@example.com", "placeholder-password", "local-123",
        user_agent=UA, session=session,
    )


@pytest.mark.parametrize("separator", ["#", "?"])
async def test_validate_credentials_preserves_b2c_flow(aiohttp_server, monkeypatch, separator):
    """Verify the actual wire URL, cookies, PKCE, and both redirect formats."""
    state = {"location": f"http://example.test/{separator}code=code-placeholder"}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        client = _client(session)
        assert await client.validate_credentials() == {"resident": {}, "occupations": []}
        assert not session.closed

    assert state["login_path"].endswith("?tx=StateProperties=abc==&p=B2C_1A_SIGNUP_SIGNIN")
    assert "tx=StateProperties=abc==&p=B2C_1A_SIGNUP_SIGNIN" in state["confirmed_path"]
    assert state["login_cookie"] == state["csrf"] == "csrf-placeholder"
    assert state["user_agent"] == UA
    assert state["login_form"]["email"] == "resident@example.com"
    assert state["authorize"]["code_challenge_method"] == "S256"
    token = state["token_forms"][0]
    assert token["code"] == "code-placeholder"
    digest = hashlib.sha256(token["code_verifier"].encode()).digest()
    assert state["authorize"]["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert state["requests"] == [("GET", "/api/v1/resident", "Bearer token-placeholder")]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unauthorized_request_refreshes_once(aiohttp_server, monkeypatch, method):
    """The retried request uses the new token and preserves its payload."""
    state = {"api_statuses": [401, 200]}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._access_token = "old-placeholder"
        client._auth._refresh_token = "refresh-placeholder"
        payload = {"value": 1} if method == "POST" else None
        await client._api_request(method, "/api/test", payload)
    assert [request[2] for request in state["requests"]] == ["Bearer old-placeholder", "Bearer new-placeholder"]
    assert state["token_forms"][0]["grant_type"] == "refresh_token"
    if payload is not None:
        assert state["payload"] == payload


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_second_unauthorized_response_does_not_retry(aiohttp_server, monkeypatch, method):
    state = {"api_statuses": [401, 401]}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._access_token = "old-placeholder"
        client._auth._refresh_token = "refresh-placeholder"
        with pytest.raises(OceaApiError, match="HTTP 401"):
            await client._api_request(method, "/api/test", {} if method == "POST" else None)
    assert len(state["requests"]) == 2
    assert len(state["token_forms"]) == 1


@pytest.mark.parametrize(
    ("state", "error"),
    [
        ({"no_cookie": True}, "csrf"),
        ({"html": "{}"}, "transId"),
        ({"login_status": 403}, "Login failed"),
        ({"login_body": '{"status":"400"}'}, "Login rejected"),
        ({"location": ""}, "No redirect"),
        ({"location": "http://example.test/#error=invalid"}, "B2C authorization failed"),
        ({"location": "http://example.test/"}, "No authorization code"),
        ({"token_status": 400}, "Token exchange failed"),
        ({"token_response": {}}, "No access token received"),
        ({"token_response": []}, "Invalid token response"),
    ],
)
async def test_authentication_errors(aiohttp_server, monkeypatch, state, error):
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        with pytest.raises(OceaAuthError, match=error):
            await _client(session).validate_credentials()


@pytest.mark.parametrize("body", ["", "not-json"])
async def test_login_empty_or_invalid_json_keeps_existing_behavior(aiohttp_server, monkeypatch, body):
    state = {"login_body": body}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await _client(session).validate_credentials()


async def test_failed_refresh_falls_back_to_full_authentication(aiohttp_server, monkeypatch):
    state = {"refresh_status": 400}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._refresh_token = "refresh-placeholder"
        client._auth.authenticate = AsyncMock()
        await client._auth.refresh_access_token()
        client._auth.authenticate.assert_awaited_once()
        assert client._auth._refresh_token is None


async def test_refresh_without_refresh_token_authenticates():
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth.authenticate = AsyncMock()
        await client._auth.refresh_access_token()
        client._auth.authenticate.assert_awaited_once()


async def test_refresh_without_access_token_fails(aiohttp_server, monkeypatch):
    state = {"refresh_response": {}}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._refresh_token = "refresh-placeholder"
        with pytest.raises(OceaAuthError, match="No access token on refresh"):
            await client._auth.refresh_access_token()


async def test_get_consumptions_targets_configured_dwelling(aiohttp_server, monkeypatch):
    state = {"api_response": []}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._access_token = "token-placeholder"
        assert await client.get_consumptions() == []
    assert state["requests"][0][1] == "/api/v1/local/local-123/dashboard/consos"


async def test_first_api_call_authenticates_when_token_is_missing(aiohttp_server, monkeypatch):
    state = {}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        assert await _client(session).get_resident() == {"resident": {}, "occupations": []}
    assert state["requests"][0][2] == "Bearer token-placeholder"


async def test_refresh_updates_rotated_refresh_token(aiohttp_server, monkeypatch):
    state = {"refresh_response": {"access_token": "new-placeholder", "refresh_token": "rotated-placeholder"}}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._refresh_token = "old-placeholder"
        await client._auth.refresh_access_token()
        assert client._auth._refresh_token == "rotated-placeholder"


@pytest.mark.parametrize("response", [None, [], "invalid"])
async def test_invalid_resident_response(aiohttp_server, monkeypatch, response):
    state = {"api_response": response}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._access_token = "token-placeholder"
        with pytest.raises(OceaApiError, match="Invalid API object"):
            await client.get_resident()


async def test_invalid_list_response(aiohttp_server, monkeypatch):
    state = {"api_response": {}}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._auth._access_token = "token-placeholder"
        with pytest.raises(OceaApiError, match="Invalid API list"):
            await client.get_pds()


async def test_authentication_cookies_are_isolated_between_sessions(aiohttp_server, monkeypatch):
    """Each independently injected session keeps its own B2C cookies."""
    state = {"cookies": ["first-placeholder", "second-placeholder"]}
    await _server(aiohttp_server, monkeypatch, state)
    async with (
        aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as first,
        aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as second,
    ):
        await _client(first).validate_credentials()
        await _client(second).validate_credentials()
        assert first.cookie_jar is not second.cookie_jar
    assert state["login_cookies"] == ["first-placeholder", "second-placeholder"]


async def test_authentication_accepts_path_scoped_csrf_cookie(aiohttp_server, monkeypatch):
    """The CSRF cookie can be scoped to the B2C tenant path."""
    state = {"cookie_path": "/tenant.onmicrosoft.com"}
    await _server(aiohttp_server, monkeypatch, state)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        await _client(session).validate_credentials()
    assert state["login_cookie"] == state["csrf"] == "csrf-placeholder"
