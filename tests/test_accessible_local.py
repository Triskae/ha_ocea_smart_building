"""Tests for Ocea dwelling access selection."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ocea_smart_building.api import OceaApiClient, OceaApiError
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
)


def _client() -> OceaApiClient:
    """Create a client without performing network requests."""
    return OceaApiClient("resident@example.com", "secret", "old-local")


def test_resolve_accessible_local_skips_forbidden_candidate() -> None:
    """A stale HTTP 403 occupation must not hide a valid dwelling."""
    client = _client()
    calls: list[str] = []

    def get_consumptions() -> list[dict[str, str]]:
        calls.append(client._local_id)
        if client._local_id == "stale-local":
            raise OceaApiError("forbidden", status_code=403)
        return []

    client.get_consumptions = get_consumptions  # type: ignore[method-assign]
    resident_data = {
        "occupations": [
            {"logementId": "stale-local"},
            {"logementId": "working-local"},
        ]
    }

    assert client.resolve_accessible_local(resident_data) == "working-local"
    assert calls == ["stale-local", "working-local"]
    assert client._local_id == "old-local"


def test_resolve_accessible_local_prefers_current_dwelling() -> None:
    """The current dwelling is retained when it is still accessible."""
    client = _client()
    client.get_consumptions = Mock(return_value=[])
    resident_data = {
        "occupations": [
            {"logementId": "other-local"},
            {"logementId": "old-local"},
        ]
    }

    assert (
        client.resolve_accessible_local(resident_data, "old-local") == "old-local"
    )
    client.get_consumptions.assert_called_once_with()


def test_resolve_accessible_local_reports_all_forbidden() -> None:
    """An account with only forbidden dwellings gets a distinct error."""
    client = _client()
    client.get_consumptions = Mock(
        side_effect=OceaApiError("forbidden", status_code=403)
    )

    with pytest.raises(OceaApiError, match="No accessible occupation") as caught:
        client.resolve_accessible_local(
            {"occupations": [{"logementId": "stale-local"}]}
        )

    assert caught.value.status_code == 403


def test_resolve_accessible_local_preserves_non_403_error() -> None:
    """Server failures must not be hidden as inaccessible occupations."""
    client = _client()
    client.get_consumptions = Mock(
        side_effect=OceaApiError("server error", status_code=500)
    )

    with pytest.raises(OceaApiError, match="server error") as caught:
        client.resolve_accessible_local(
            {"occupations": [{"logementId": "working-local"}]}
        )

    assert caught.value.status_code == 500


async def test_coordinator_maps_forbidden_dwelling_to_reauth(
    hass: HomeAssistant,
) -> None:
    """A refresh-time 403 asks Home Assistant to reauthenticate."""
    client = Mock()
    client.get_consumptions.side_effect = OceaApiError(
        "forbidden", status_code=403
    )
    client.get_precise_consumptions.return_value = []
    coordinator = OceaDataUpdateCoordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_keeps_other_api_failures_as_update_errors(
    hass: HomeAssistant,
) -> None:
    """A non-403 API failure remains a regular update failure."""
    client = Mock()
    client.get_consumptions.side_effect = OceaApiError(
        "server error", status_code=500
    )
    client.get_precise_consumptions.return_value = []
    coordinator = OceaDataUpdateCoordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
