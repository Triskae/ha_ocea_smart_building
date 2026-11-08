"""Tests for the standalone OCEA CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
import requests
from aiohttp import web

import ocea_cli


def response(data, status=200):
    """Create an API response without network access."""
    result = MagicMock()
    result.status_code = status
    result.json.return_value = data
    return result


def cli_args(start=None, end=None):
    """Return CLI arguments using fictitious credentials."""
    return argparse.Namespace(
        email="resident@example.invalid",
        password="placeholder password & symbols",
        dump_tokens=False,
        verbose=True,
        start_date=start,
        end_date=end,
    )


def mock_cli_session(monkeypatch, results, dwelling_results=None):
    """Prepare a session and authentication for one CLI invocation."""
    session = MagicMock()
    session.__enter__.return_value = session
    if dwelling_results is None:
        dwelling_results = [response({"fluidesRestitues": []})]
    session.request.side_effect = [*results[:2], *dwelling_results, *results[2:]]
    monkeypatch.setattr(ocea_cli.requests, "Session", lambda: session)
    monkeypatch.setattr(
        ocea_cli, "authenticate", lambda *_args: {"access_token": "placeholder-token"}
    )
    return session


def test_fluid_label_recognizes_exact_cetc_code():
    """Only the actual OCEA code is recognized as CETC heating."""
    assert ocea_cli.fluid_label("Cetc") == "Heating (CETC)"
    assert ocea_cli.fluid_label("CETC") == "Other fluid"


@pytest.mark.usefixtures("local_http_only")
async def test_real_daily_http_request(aiohttp_server, monkeypatch):
    """The standalone requests client sends the correct JSON and escapes PDS IDs."""
    seen = []

    async def handler(request):
        seen.append((request.raw_path, request.headers["Authorization"], await request.json()))
        return web.json_response({"unite": "kWh", "consommations": []})

    app = web.Application()
    app.router.add_post("/api/v1/local/{local}/pds/{pds:.+}/conso", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(ocea_cli, "API_BASE", str(server.make_url("/")).rstrip("/"))
    payload = ocea_cli.period_payload(date(2026, 2, 1), date(2026, 2, 3))
    with requests.Session() as session:
        result = await asyncio.to_thread(
            ocea_cli.fetch_pds_consumption,
            session, "placeholder-token", "local", "pds/1", payload,
        )
    assert result == {"unite": "kWh", "consommations": []}
    assert seen == [
        ("/api/v1/local/local/pds/pds%2F1/conso", "Bearer placeholder-token", payload)
    ]


@pytest.mark.usefixtures("local_http_only")
async def test_real_dwelling_http_request(aiohttp_server, monkeypatch):
    """Encode both path segments and send the daily request unchanged."""
    seen = []

    async def handler(request):
        seen.append((request.raw_path, await request.json()))
        return web.json_response({"unite": "m3", "consommations": []})

    app = web.Application()
    app.router.add_post("/api/v1/local/{local}/conso/{fluid:.+}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(ocea_cli, "API_BASE", str(server.make_url("/")).rstrip("/"))
    payload = ocea_cli.period_payload(date(2026, 2, 1), date(2026, 2, 3))
    with requests.Session() as session:
        await asyncio.to_thread(
            ocea_cli.fetch_dwelling_consumption,
            session, "placeholder-token", "local/1", "fluid/1", payload,
        )
    assert seen == [("/api/v1/local/local%2F1/conso/fluid%2F1", payload)]


def test_daily_pds_requests_and_safe_output(monkeypatch, capsys, caplog):
    """Request every PDS, including heating and unknown fluids, without exposing IDs."""
    session = mock_cli_session(
        monkeypatch,
        [
            response({"resident": {"nom": "private-name"}, "occupations": [
                {"logementId": "private-local"}, {"logementId": "another-local"}
            ]}),
            response([{"fluide": "Cetc", "valeur": "1,5", "unite": "kWh"}]),
            response([
                {"id": "private-water", "fluide": "EauFroide"},
                {"id": "private-heat", "fluide": "Cetc"},
                {"id": "private-other", "fluide": "Gaz"},
            ]),
            response({"unite": "m3", "consommations": [
                {"date": "2026-02-01T00:00:00", "valeur": 0.1, "fuiteEstimee": 0,
                 "isEstimated": False, "consentement": True, "type": "ConsommationEauResponse"}
            ]}),
            response({"unite": "kWh", "consommations": [
                {"date": "2026-02-02", "valeur": 1.2, "valeurCoefficientee": 1.5,
                 "isEstimated": True, "consentement": True, "type": "ConsommationRfcResponse"}
            ]}),
            response({"unite": "m3", "consommations": []}),
        ],
    )
    caplog.set_level(logging.DEBUG, logger="ocea")
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 3))) == 0
    calls = session.request.call_args_list
    assert [call.args[0] for call in calls] == [
        "GET", "GET", "GET", "GET", "POST", "POST", "POST"
    ]
    assert [call.args[1] for call in calls[4:]] == [
        f"{ocea_cli.API_BASE}/api/v1/local/private-local/pds/{pds}/conso"
        for pds in ("private-water", "private-heat", "private-other")
    ]
    assert all(call.kwargs["json"] == {
        "debut": "2026-02-01T00:00:00.000Z",
        "fin": "2026-02-03T23:59:59.999Z",
        "granularity": "Day",
    } for call in calls[4:])
    output = capsys.readouterr().out + caplog.text
    assert "PDS 1: Cold water" in output
    assert "PDS 2: Heating (CETC)" in output
    assert "PDS 3: Gas" in output
    assert "2026-02-01T00:00:00: 0.1 m3" in output
    assert "2026-02-02: 1.2 kWh" in output
    assert "corrected: 1.5 kWh" in output
    assert "estimated: yes" in output
    assert "type: ConsommationRfcResponse" in output
    assert "No readings returned" in output
    assert "Some dates" in output
    for private in (
        "private-name", "private-local", "another-local", "private-water", "private-heat",
        "private-other", "placeholder-token", "resident@example.invalid", "placeholder password",
    ):
        assert private not in output


def test_dwelling_consumption_discovery_and_safe_output(monkeypatch, capsys, caplog):
    """Explore distinct supported fluids even when there are no PDS."""
    session = mock_cli_session(
        monkeypatch,
        [
            response({"occupations": [{"logementId": "private-local"}]}),
            response([]),
            response([]),
        ],
        dwelling_results=[
            response({"fluidesRestitues": [
                {"fluide": "EauFroide"}, {"fluide": "EauFroide"},
                {"fluide": "Cetc"}, {"fluide": "private-fluid"},
            ]}),
            response({"unite": "m3", "consommations": [{
                "date": "2026-02-01", "valeur": 0.1, "consentement": True,
                "type": "ConsommationEauResponse", "private-key": "private-value",
            }], "private-top": "private-value"}),
            response({"unite": "kWh", "consommations": []}),
        ],
    )
    caplog.set_level(logging.DEBUG, logger="ocea")
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 2))) == 0
    calls = session.request.call_args_list
    assert [call.args[1] for call in calls[3:5]] == [
        f"{ocea_cli.API_BASE}/api/v1/local/private-local/conso/EauFroide",
        f"{ocea_cli.API_BASE}/api/v1/local/private-local/conso/Cetc",
    ]
    assert all(call.kwargs["json"] == ocea_cli.period_payload(
        date(2026, 2, 1), date(2026, 2, 2)
    ) for call in calls[3:5])
    output = capsys.readouterr().out + caplog.text
    assert "Dwelling: Cold water (EauFroide)" in output
    assert "Dwelling: Heating (CETC) (Cetc)" in output
    assert "Reported unit: m3" in output
    assert "Field consentement: 1/1" in output
    assert "Field isEstimated: 0/1" in output
    assert "Readings with other fields: 1" in output
    assert "Response contains other fields." in output
    assert "No PDS found" in output
    for private in ("private-local", "private-fluid", "private-key", "private-top", "private-value"):
        assert private not in output


def test_dwelling_failures_do_not_stop_other_sources(monkeypatch, capsys, caplog):
    """Report one failed aggregate response and continue with other fluids and PDS."""
    mock_cli_session(
        monkeypatch,
        [
            response({"occupations": [{"logementId": "local"}]}),
            response([]),
            response([{"id": "pds-1", "fluide": "EauFroide"}]),
            response({"unite": "m3", "consommations": []}),
        ],
        dwelling_results=[
            response({"fluidesRestitues": [
                {"fluide": "EauFroide"}, {"fluide": "EauChaude"},
            ]}),
            response("private response", 503),
            response({"unite": "m3", "consommations": []}),
        ],
    )
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 1))) == 1
    assert "Dwelling Cold water: HTTP 503" in caplog.text
    output = capsys.readouterr().out
    assert "Dwelling: Hot water" in output
    assert "PDS 1: Cold water" in output
    assert "private response" not in output + caplog.text


def test_invalid_dwelling_discovery_does_not_stop_pds(monkeypatch, capsys, caplog):
    """An invalid dwelling response remains an error while PDS are explored."""
    mock_cli_session(
        monkeypatch,
        [
            response({"occupations": [{"logementId": "local"}]}),
            response([]),
            response([{"id": "pds", "fluide": "EauFroide"}]),
            response({"unite": "m3", "consommations": []}),
        ],
        dwelling_results=[response({"fluidesRestitues": None})],
    )
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 1))) == 1
    assert "Dwelling fluids: Invalid dwelling fluids response" in caplog.text
    output = capsys.readouterr().out
    assert "PDS 1: Cold water" in output
    assert "No dwelling fluids available" not in output


def test_default_period_uses_local_month(monkeypatch, capsys):
    """Default bounds use the local date without reporting today as missing."""
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 2, 29, 12, tzinfo=timezone.utc)

        def astimezone(self, tz=None):
            return self

    monkeypatch.setattr(ocea_cli, "datetime", FixedDatetime)
    session = mock_cli_session(monkeypatch, [
        response({"occupations": [{"logementId": "local"}]}),
        response([]),
        response([{"id": "pds"}]),
        response({"unite": "m3", "consommations": [
            {"date": f"2024-02-{day:02d}", "valeur": 0}
            for day in range(1, 29)
        ]}),
    ])
    assert ocea_cli.main(cli_args()) == 0
    assert session.request.call_args.kwargs["json"] == {
        "debut": "2024-02-01T00:00:00.000Z",
        "fin": "2024-02-29T23:59:59.999Z",
        "granularity": "Day",
    }
    assert "Some dates" not in capsys.readouterr().out


def test_pds_errors_are_reported_and_do_not_stop_other_meters(monkeypatch, capsys, caplog):
    """Failed and malformed PDS do not suppress later readings or leak error details."""
    mock_cli_session(monkeypatch, [
        response({"occupations": [{"logementId": "private-local"}]}),
        response([]),
        response([{"id": "pds1"}, {"id": "pds2"}, {"id": "pds3"}, {"id": "pds4"}]),
        response("private response", 500),
        response({"consommations": [{"date": "2026-02-01", "valeur": "private response"}]}),
        requests.ConnectionError("private-url and placeholder-token"),
        response({"unite": "kWh", "consommations": [{"date": "2026-02-01", "valeur": 2}]}),
    ])
    with caplog.at_level(logging.DEBUG, logger="ocea"):
        assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 1))) == 1
    output = capsys.readouterr().out + caplog.text
    assert "PDS 1: HTTP 500" in output
    assert "PDS 2: Invalid consumption value" in output
    assert "PDS 3: network request failed" in output
    assert "2026-02-01: 2 kWh" in output
    for private in ("private response", "private-local", "private-url", "placeholder-token"):
        assert private not in output


@pytest.mark.parametrize("data", [
    {},
    {"consommations": None},
    {"consommations": ["private"]},
    {"consommations": [{"date": "private", "valeur": 1}]},
    {"consommations": [{"date": "2026-02-01", "valeur": True}]},
    {"consommations": [{"date": "2026-02-01", "valeur": "NaN"}]},
    {"consommations": [{"date": "2026-02-01", "valeur": 1, "consentement": "private"}]},
])
def test_invalid_consumption_response_is_explicit(data):
    """Unexpected structures are errors, never silently treated as zero."""
    with pytest.raises(ocea_cli.OCEACliError, match="Invalid consumption"):
        ocea_cli.display_consumptions(data, date(2026, 2, 1), date(2026, 2, 2), date(2026, 2, 3))


def test_sparse_monthly_and_missing_values(capsys):
    """Do not invent daily values or claim the server actually returns daily data."""
    ocea_cli.display_consumptions(
        {"unite": "kWh", "consommations": [
            {"date": "2026-01-01T00:00:00", "valeur": None},
            {"date": "2026-02-01T00:00:00", "valeur": 0},
        ]},
        date(2026, 1, 1),
        date(2026, 2, 3),
        date(2026, 2, 4),
    )
    output = capsys.readouterr().out
    assert "2026-01-01T00:00:00: unavailable kWh" in output
    assert "2026-02-01T00:00:00: 0 kWh" in output
    assert "appear monthly" in output
    assert "daily availability is not confirmed" in output


@pytest.mark.parametrize(
    ("start", "end", "today", "dates", "message"),
    [
        (date(2026, 2, 27), date(2026, 3, 1), date(2026, 3, 1),
         ["2026-02-27", "2026-02-28"], None),
        (date(2026, 2, 27), date(2026, 3, 3), date(2026, 3, 1),
         ["2026-02-27", "2026-02-28"], None),
        (date(2026, 2, 27), date(2026, 3, 3), date(2026, 3, 1),
         ["2026-02-28"], "Some dates"),
        (date(2026, 2, 27), date(2026, 2, 28), date(2026, 3, 1),
         ["2026-02-28"], "Some dates"),
        (date(2026, 3, 1), date(2026, 3, 1), date(2026, 3, 1),
         [], "No completed days"),
        (date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 1),
         [], "No completed days"),
        (date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 1),
         [], "No readings returned"),
    ],
)
def test_missing_days_only_include_completed_days(start, end, today, dates, message, capsys):
    """Ignore today and the future, but still report missing past readings."""
    data = {"unite": "m3", "consommations": [
        {"date": reading_date, "valeur": 1} for reading_date in dates
    ]}
    ocea_cli.display_consumptions(data, start, end, today)
    output = capsys.readouterr().out
    if message is None:
        assert "Some dates" not in output
        assert "No readings" not in output
    else:
        assert message in output


def test_missing_pds_identifier_does_not_stop_later_pds(monkeypatch, capsys, caplog):
    """Malformed discovery data is reported while valid PDS are still processed."""
    session = mock_cli_session(monkeypatch, [
        response({"occupations": [{"logementId": "local"}]}),
        response([]),
        response([{"fluide": "Cetc"}, {"id": "pds-2", "fluide": "Cetc"}]),
        response({"unite": "kWh", "consommations": [{"date": "2026-02-01", "valeur": 1}]}),
    ])
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 1))) == 1
    assert session.request.call_count == 5
    assert "missing identifier" in caplog.text
    assert "2026-02-01: 1 kWh" in capsys.readouterr().out


def test_dashboard_failure_does_not_stop_pds(monkeypatch, capsys, caplog):
    """A dashboard error does not prevent the requested PDS exploration."""
    mock_cli_session(monkeypatch, [
        response({"occupations": [{"logementId": "local"}]}),
        response("private response", 503),
        response([{"id": "pds-1", "fluide": "Rfc"}]),
        response({"unite": "U", "consommations": [
            {"date": "2026-02-01", "valeur": 1, "type": "ConsommationResponse"}
        ]}),
    ])
    assert ocea_cli.main(cli_args(date(2026, 2, 1), date(2026, 2, 1))) == 1
    assert "Dashboard: HTTP 503" in caplog.text
    output = capsys.readouterr().out
    assert "PDS 1: Heating allocation (RFC)" in output
    assert "2026-02-01: 1 U" in output
    assert "type: ConsommationResponse" in output


def test_dump_tokens_requires_explicit_flag(monkeypatch, capsys):
    """The existing opt-in token output remains available."""
    mock_cli_session(monkeypatch, [
        response({"occupations": [{"logementId": "local"}]}),
        response([]),
        response([]),
    ])
    args = cli_args()
    args.dump_tokens = True
    assert ocea_cli.main(args) == 0
    assert "placeholder-token" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ["--start-date", "2026-02-30", "--end-date", "2026-03-01"],
    ["--start-date", "2026-02-01"],
    ["--start-date", "2026-03-01", "--end-date", "2026-02-01"],
    ["--start-date", "20260201", "--end-date", "2026-03-01"],
])
def test_invalid_date_arguments_fail_before_network(arguments):
    """Invalid or incomplete bounds are rejected before authentication."""
    result = subprocess.run(
        [sys.executable, "ocea_cli.py", "--email", "x", "--password", "x", *arguments],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
    assert "Step 1/4" not in result.stderr


def test_authentication_verbose_does_not_log_secrets(caplog):
    """The original B2C exchange remains usable without logging its sensitive fields."""
    session = MagicMock()
    session.cookies.get.return_value = "fake-csrf"
    authorize = response(None)
    authorize.text = '{"transId":"fake-transaction"}'
    authorize.url = "https://example.invalid/?fake-private-url"
    confirmed = response(None)
    confirmed.headers = {"Location": "https://example.invalid/#code=fake-auth-code"}
    session.get.side_effect = [authorize, confirmed]
    session.prepare_request.side_effect = lambda request: request.prepare()
    accepted = response({"status": "200"})
    accepted.text = '{"status":"200"}'
    accepted.headers = {"Set-Cookie": "fake-cookie"}
    session.send.return_value = accepted
    session.post.return_value = response({"access_token": "fake-access", "refresh_token": "fake-refresh"})
    with caplog.at_level(logging.DEBUG, logger="ocea"):
        result = ocea_cli.authenticate(session, "resident@example.invalid", "placeholder password & symbols")
    assert result["access_token"] == "fake-access"
    assert "Got authorization code" in caplog.text
    for private in (
        "resident@example.invalid", "placeholder", "fake-csrf", "fake-transaction", "fake-private-url",
        "fake-auth-code", "fake-cookie", "fake-access", "fake-refresh",
    ):
        assert private not in caplog.text


def test_authentication_error_does_not_log_response_body(caplog):
    """An authentication failure must not print server-supplied account data."""
    session = MagicMock()
    session.cookies.get.return_value = "fake-csrf"
    authorize = response(None)
    authorize.text = '{"transId":"fake-transaction"}'
    authorize.url = "https://example.invalid/?fake-private-url"
    session.get.return_value = authorize
    session.prepare_request.side_effect = lambda request: request.prepare()
    rejected = response(None, 400)
    rejected.text = "fake-private-response"
    session.send.return_value = rejected
    with caplog.at_level(logging.DEBUG, logger="ocea"), pytest.raises(SystemExit):
        ocea_cli.authenticate(session, "resident@example.invalid", "placeholder")
    assert "HTTP 400" in caplog.text
    for private in ("fake-private-response", "fake-private-url", "fake-csrf", "resident@example.invalid"):
        assert private not in caplog.text
