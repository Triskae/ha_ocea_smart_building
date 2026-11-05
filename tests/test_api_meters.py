"""Tests for individual Ocea water-meter API calls."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from custom_components.ocea_smart_building.api import OceaApiClient, OceaApiError


def _client() -> OceaApiClient:
    """Create an API client without performing authentication."""
    client = OceaApiClient("resident@example.com", "password", "local-123")
    client._access_token = "access-token"
    return client


def test_get_pds_returns_points_for_configured_local() -> None:
    """PDS discovery uses the configured dwelling and authenticated GET."""
    client = _client()
    response = Mock()
    response.status_code = 200
    response.json.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"id": "pds-cold-2", "fluide": "EauFroide"},
    ]
    client._session.get = Mock(return_value=response)

    assert client.get_pds() == response.json.return_value

    client._session.get.assert_called_once()
    request_url = client._session.get.call_args.args[0]
    request_headers = client._session.get.call_args.kwargs["headers"]
    assert request_url.endswith("/api/v1/local/local-123/pds")
    assert request_headers["Authorization"] == "Bearer access-token"


def test_get_appareils_posts_all_pds_ids() -> None:
    """Device lookup sends the discovered PDS IDs in the JSON payload."""
    client = _client()
    response = Mock()
    response.status_code = 200
    response.json.return_value = [
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    ]
    client._session.post = Mock(return_value=response)

    assert (
        client.get_appareils(["pds-cold-1", "pds-cold-2"])
        == response.json.return_value
    )

    client._session.post.assert_called_once()
    call = client._session.post.call_args
    assert call.args[0].endswith("/api/v1/local/appareils")
    assert call.kwargs["json"] == {"pdsIds": ["pds-cold-1", "pds-cold-2"]}
    assert call.kwargs["headers"]["Authorization"] == "Bearer access-token"


@pytest.mark.parametrize("method", ["get", "post"])
def test_api_error_does_not_expose_response_body(method: str) -> None:
    """API error messages must not leak response data into Home Assistant logs."""
    client = _client()
    response = Mock()
    response.status_code = 500
    response.text = "private resident data"
    setattr(client._session, method, Mock(return_value=response))

    with pytest.raises(OceaApiError, match="HTTP 500") as err:
        if method == "get":
            client._api_get("/api/v1/test")
        else:
            client._api_post("/api/v1/test", {})

    assert "private resident data" not in str(err.value)


def test_get_pds_consumption_posts_period_and_granularity() -> None:
    """Individual consumption queries target one PDS at a time."""
    client = _client()
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "unite": "m3",
        "consommations": [{"date": "2026-09-01", "valeur": 0.123}],
    }
    client._session.post = Mock(return_value=response)

    payload = {
        "debut": "2026-09-01T00:00:00.000Z",
        "fin": "2026-09-16T00:00:00.000Z",
        "granularity": "Day",
    }

    assert (
        client.get_pds_consumption("pds-cold-1", payload)
        == response.json.return_value
    )

    call = client._session.post.call_args
    assert call.args[0].endswith("/api/v1/local/local-123/pds/pds-cold-1/conso")
    assert call.kwargs["json"] == payload
    assert call.kwargs["headers"]["Authorization"] == "Bearer access-token"
