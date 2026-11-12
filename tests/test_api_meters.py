"""Tests for individual Ocea water-meter API calls."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from custom_components.ocea_smart_building.const import UA
from custom_components.ocea_smart_building.ocea import OceaApiClient, OceaApiError

pytestmark = pytest.mark.usefixtures("local_http_only")


@pytest.mark.parametrize(
    ("method", "path", "payload", "result"),
    [
        ("pds", "/api/v1/local/local-123/pds", None, [{"id": "pds-1"}]),
        ("appareils", "/api/v1/local/appareils", {"pdsIds": ["pds-1"]}, []),
        ("consumption", "/api/v1/local/local-123/pds/pds-1/conso", {"granularity": "Day"}, {}),
    ],
)
async def test_meter_endpoints(aiohttp_server, monkeypatch, method, path, payload, result):
    """Meter endpoints send the expected URL, token, and JSON payload."""
    seen = []

    async def handler(request):
        seen.append((request.path, request.headers["Authorization"], request.method))
        if payload is not None:
            assert await request.json() == payload
        return web.json_response(result)

    app = web.Application()
    app.router.add_route("*", path, handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE", str(server.make_url("/")).rstrip("/")
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("resident@example.com", "placeholder", "local-123", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        if method == "pds":
            actual = await client.get_pds()
        elif method == "appareils":
            actual = await client.get_appareils(["pds-1"])
        else:
            actual = await client.get_pds_consumption("pds-1", payload)
    assert actual == result
    assert seen == [(path, "Bearer placeholder", "GET" if payload is None else "POST")]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_api_error_does_not_expose_response_body(aiohttp_server, monkeypatch, method):
    """API errors never include response data in Home Assistant logs."""
    async def handler(request):
        return web.Response(status=500, text="private resident data")

    app = web.Application()
    app.router.add_route("*", "/test", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE", str(server.make_url("/")).rstrip("/")
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("resident@example.com", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        with pytest.raises(OceaApiError, match="HTTP 500") as error:
            await client._api_request(method, "/test", {} if method == "POST" else None)
    assert "private resident data" not in str(error.value)


async def test_invalid_json_does_not_expose_response_body(aiohttp_server, monkeypatch):
    """Malformed JSON is an API error and does not include the response body."""
    async def handler(request):
        return web.Response(text="private resident data")

    app = web.Application()
    app.router.add_get("/test", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE", str(server.make_url("/")).rstrip("/")
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("resident@example.com", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        with pytest.raises(OceaApiError, match="Invalid API JSON response") as error:
            await client._api_request("GET", "/test")
    assert "private resident data" not in str(error.value)
