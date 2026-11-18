"""Tests for individual Ocea water-meter API calls."""

from __future__ import annotations

import logging
from decimal import Decimal
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.ocea_smart_building.const import UA
from custom_components.ocea_smart_building.ocea import OceaApiClient, OceaApiError
from custom_components.ocea_smart_building.ocea.models import (
    OceaGenericConsumptionData,
    OceaRfcConsumptionData,
    OceaUnclassifiedConsumptionData,
    OceaWaterConsumptionData,
)

pytestmark = pytest.mark.usefixtures("local_http_only")


@pytest.mark.parametrize(
    ("method", "path", "payload", "result"),
    [
        ("pds", "/api/v1/local/local-123/pds", None, [{"id": "pds-1"}]),
        ("appareils", "/api/v1/local/appareils", {"pdsIds": ["pds-1"]}, []),
        ("consumption", "/api/v1/local/local-123/pds/pds-1/conso", {
            "debut": "2026-01-01", "fin": "2026-01-31", "granularity": "Day"
        }, {}),
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
        client = OceaApiClient("resident@example.com", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        if method == "pds":
            actual = await client.get_pds("local-123")
        elif method == "appareils":
            actual = await client.get_appareils(payload)
        else:
            actual = await client.get_pds_consumption("local-123", "pds-1", payload)
    assert ([item.raw for item in actual] if isinstance(actual, list) else actual.raw) == result
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


async def test_one_client_targets_explicit_encoded_identifiers(aiohttp_server, monkeypatch):
    """Local and PDS identifiers are independent URL path segments."""
    paths = []

    async def handler(request):
        paths.append(request.raw_path)
        return web.json_response({} if request.method == "POST" else [])

    app = web.Application()
    app.router.add_route("*", "/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        await client.get_consumptions("local/one")
        await client.get_pds("local two")
        await client.get_pds_consumption(
            "local/one", "pds/?", {"debut": "start", "fin": "end", "granularity": "Day"}
        )

    assert paths == [
        "/api/v1/local/local%2Fone/dashboard/consos",
        "/api/v1/local/local%20two/pds",
        "/api/v1/local/local%2Fone/pds/pds%2F%3F/conso",
    ]


@pytest.mark.parametrize(
    ("method", "response"),
    [
        ("dashboard", [{"fluide": False}]),
        ("pds", [{"id": True}]),
        ("pds", [{"fluide": False}]),
        ("devices", [{"pdsId": []}]),
        ("devices", [{"pdsId": True}]),
        ("consumption", {"unite": []}),
        ("consumption", {"consommations": {}}),
        ("consumption", {"consommations": [None]}),
        ("consumption", {"consommations": [{"consentement": "yes"}]}),
    ],
)
async def test_invalid_known_fields_are_rejected(aiohttp_server, monkeypatch, method, response):
    """Untrusted JSON is validated before it reaches typed consumers."""
    async def handler(request):
        return web.json_response(response)

    app = web.Application()
    app.router.add_route("*", "/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        with pytest.raises(OceaApiError):
            if method == "dashboard":
                await client.get_consumptions("local")
            elif method == "pds":
                await client.get_pds("local")
            elif method == "devices":
                await client.get_appareils({"pdsIds": []})
            else:
                await client.get_pds_consumption(
                    "local", "pds", {"debut": "start", "fin": "end", "granularity": "Day"}
                )


async def test_debug_shape_never_logs_unknown_keys_or_values(
    aiohttp_server, monkeypatch, caplog
):
    """Even nested unknown keys and known sensitive fields never expose account data."""
    async def handler(request):
        return web.json_response({
            "resident": {"prenom": "secret-name", "secret-key": "secret-value"},
            "occupations": [
                {"logementId": "secret-local", "secret-nested": {"token": "secret-token"}}
            ],
            "secret-top": ["secret-item"],
        })

    app = web.Application()
    app.router.add_get("/api/v1/resident", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    logger = "custom_components.ocea_smart_building.ocea.client"
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("secret-email", "secret-password", user_agent=UA, session=session)
        client._auth._access_token = "secret-token"
        with caplog.at_level(logging.DEBUG, logger=logger):
            await client.get_resident()

    messages = [record.getMessage() for record in caplog.records if record.name == logger]
    assert len(messages) == 1
    assert "resident shape:" in messages[0]
    assert "$.resident.prenom:str" in messages[0]
    assert "unknown#" in messages[0]
    assert "secret" not in messages[0]


async def test_shape_diagnostics_are_skipped_when_debug_is_disabled(aiohttp_server, monkeypatch):
    """Do not traverse responses for diagnostics unless debugging is enabled."""
    async def handler(request):
        return web.json_response([])

    app = web.Application()
    app.router.add_get("/api/v1/local/{local}/pds", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        with patch.object(client, "_log_response") as log_response:
            await client.get_pds("local")
        log_response.assert_not_called()


async def test_debug_shape_is_bounded_and_unknown_hashes_are_instance_scoped(caplog):
    """Diagnostics truncate wide and deep data and do not correlate accounts."""
    async with aiohttp.ClientSession() as session:
        first = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        second = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        data = {
            "resident": {"resident": {"resident": {"resident": {"secret-deep": "secret"}}}},
            **{f"secret-{index}": "secret" for index in range(60)},
        }
        with caplog.at_level(logging.DEBUG, logger=first.__class__.__module__):
            first._log_response("resident", data)
            second._log_response("resident", data)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert messages[0] != messages[1]
    assert all("secret" not in message and len(message.split(", ")) <= 40 for message in messages)


async def test_debug_reports_safe_formats_without_logging_values(caplog):
    """Dates and decimal formats are useful without revealing their values."""
    data = {
        "unite": "m3",
        "fluide": "EauFroide",
        "consommations": [
            {"date": "2026-01-02", "valeur": "123,45", "type": "ConsommationEauResponse"},
            {"date": "2026-01-02T03:04:05Z", "valeur": "678.90"},
            {"date": "secret-date", "valeur": "secret-value"},
        ],
    }
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        with caplog.at_level(logging.DEBUG, logger=client.__class__.__module__):
            client._log_response("consumption", data)

    message = caplog.records[-1].getMessage()
    assert "str(date)" in message
    assert "str(datetime)" in message
    assert "str(decimal-comma)" in message
    assert "str(decimal-dot-or-integer)" in message
    assert "str(other-format)" in message
    assert "str=m3" in message
    assert "str=EauFroide" in message
    assert "str=ConsommationEauResponse" in message
    for private in ("2026-01", "123,45", "678.90", "secret"):
        assert private not in message


@pytest.mark.parametrize(
    ("method", "response"),
    [
        ("dashboard", [{"fluide": "EauFroide", "valeur": "2,5", "extra": "private"}]),
        ("pds", [{"id": None, "fluide": None, "fce": "unused"}]),
        ("devices", [{"pdsId": "pds-1", "numeroSerie": {"unused": "value"}}]),
        ("consumption", {"unite": None, "consommations": [{"extra": "private"}]}),
    ],
)
async def test_unknown_fields_and_optional_values_are_retained(
    aiohttp_server, monkeypatch, method, response
):
    """Contract validation does not discard extra API fields or optional nulls."""
    async def handler(request):
        return web.json_response(response)

    app = web.Application()
    app.router.add_route("*", "/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        if method == "dashboard":
            result = await client.get_consumptions("local")
        elif method == "pds":
            result = await client.get_pds("local")
        elif method == "devices":
            result = await client.get_appareils({"pdsIds": []})
        else:
            result = await client.get_pds_consumption(
                "local", "pds", {"debut": "start", "fin": "end", "granularity": "Day"}
            )
    assert ([item.raw for item in result] if isinstance(result, list) else result.raw) == response


async def test_consumption_variants_and_unclassified_items(
    aiohttp_server, monkeypatch
):
    """Keep mixed variants and unclassified items without losing API fields."""
    response = {
        "unite": "m3",
        "consommations": [
            {"type": "ConsommationResponse", "valeur": 1, "isEstimated": True},
            {
                "type": "ConsommationEauResponse",
                "valeur": 2,
                "fuiteEstimee": "0,1",
                "isEstimated": False,
            },
            {
                "type": "ConsommationRfcResponse",
                "valeur": 3,
                "valeurCoefficientee": None,
                "emplacement": "synthetic",
            },
            {"type": "FutureResponse", "valeur": 4, "extra": "synthetic"},
            {"valeur": 5, "fuiteEstimee": 0.2},
            {"type": None, "valeur": 6, "isEstimated": "unverified"},
        ],
    }

    async def handler(request):
        return web.json_response(response)

    app = web.Application()
    app.router.add_post("/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        result = await client.get_pds_consumption(
            "local", "pds", {"debut": "start", "fin": "end", "granularity": "Day"}
        )

    assert result.raw == response
    assert [type(item) for item in result.items] == [
        OceaGenericConsumptionData,
        OceaWaterConsumptionData,
        OceaRfcConsumptionData,
        OceaUnclassifiedConsumptionData,
        OceaUnclassifiedConsumptionData,
        OceaUnclassifiedConsumptionData,
    ]
    assert result.unit == "m3"
    assert result.items[0].value == 1
    assert result.items[1].estimated_leak == Decimal("0.1")
    assert result.items[2].corrected_value is None
    assert result.items[2].location == "synthetic"
    assert result.items[4].estimated_leak == Decimal("0.2")
    assert result.items[5].is_estimated == "unverified"
    assert "extra" not in repr(result)


async def test_invalid_reading_fields_do_not_discard_the_series(aiohttp_server, monkeypatch):
    """Keep raw data and valid items when individual readings contain invalid fields."""
    data = {"consommations": [
        {"date": "2026-01-01", "valeur": 1},
        {"date": False, "valeur": 2},
        {"date": "2026-01-03", "valeur": False},
        {"date": "2026-01-04", "valeur": "invalid"},
    ]}

    async def handler(request):
        return web.json_response(data)

    app = web.Application()
    app.router.add_post("/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        result = await client.get_pds_consumption(
            "local", "pds", {"debut": "start", "fin": "end", "granularity": "Day"}
        )
    assert result.raw == data
    assert result.items[0].value == Decimal(1)
    assert result.items[1].date is None
    assert result.items[2].value is None
    assert result.items[3].value is None


@pytest.mark.parametrize("kind", ["ConsommationEauResponse", "FutureResponse", None])
async def test_invalid_leak_estimate_is_rejected_for_all_types(
    aiohttp_server, monkeypatch, kind
):
    """Unclassified items get the same leak-field validation as water items."""
    async def handler(request):
        return web.json_response({"consommations": [{"type": kind, "fuiteEstimee": False}]})

    app = web.Application()
    app.router.add_post("/api/{path:.*}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.ocea_smart_building.ocea.client.API_BASE",
        str(server.make_url("/")).rstrip("/"),
    )
    async with aiohttp.ClientSession() as session:
        client = OceaApiClient("synthetic", "placeholder", user_agent=UA, session=session)
        client._auth._access_token = "placeholder"
        with pytest.raises(OceaApiError, match="Invalid API field: fuiteEstimee"):
            await client.get_pds_consumption(
                "local", "pds", {"debut": "start", "fin": "end", "granularity": "Day"}
            )
