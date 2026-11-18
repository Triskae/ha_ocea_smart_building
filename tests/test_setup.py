"""Tests for integration setup lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building import PLATFORMS, async_setup_entry
from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_stores_coordinator_in_entry_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Test the coordinator is stored on the loaded config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OceaApiClient"),
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator"
        ) as coordinator_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as forward_entry_setups,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh = AsyncMock()
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data is coordinator_class.return_value
    forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_closes_client_when_first_refresh_fails(
    hass: HomeAssistant,
) -> None:
    """Test a failed initial refresh does not leak the HTTP session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator"
        ) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = (
            RuntimeError("refresh failed")
        )

        with pytest.raises(RuntimeError, match="refresh failed"):
            await async_setup_entry(hass, entry)

    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_detaches_session_when_cancelled(hass: HomeAssistant) -> None:
    """Cancellation during first refresh releases the isolated session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OceaDataUpdateCoordinator", autospec=True) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await async_setup_entry(hass, entry)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_detaches_session_when_platform_setup_fails(
    hass: HomeAssistant,
) -> None:
    """A platform setup error also releases the session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OceaDataUpdateCoordinator", autospec=True),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
            side_effect=RuntimeError("platform setup failed"),
        ),
        pytest.raises(RuntimeError, match="platform setup failed"),
    ):
        await async_setup_entry(hass, entry)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_session_is_detached_after_entry_unload(hass: HomeAssistant) -> None:
    """The isolated session is alive during setup and detached after unload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OceaDataUpdateCoordinator", autospec=True),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock),
        patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        session = client_class.call_args.kwargs["session"]
        assert not session.closed
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert session.closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_removes_sensor_update_listener(hass: HomeAssistant) -> None:
    """The dynamic sensor listener is removed with the config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ocea_smart_building.OceaApiClient", autospec=True) as client:
        client.return_value.get_pds.return_value = [
            {"id": "pds-1", "fluide": "EauFroide"}
        ]
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = [
            {"fluide": "EauFroide", "valeur": "1"}
        ]
        client.return_value.get_pds_consumption.return_value = {"consommations": []}

        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        registry = er.async_get(hass)
        assert not any(
            entity.unique_id.endswith("_fuite")
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        data = coordinator.data
        reading = data.meters["pds-1"]
        coordinator.async_set_updated_data(
            replace(data, meters={"pds-1": replace(reading, estimated_leak=0.1)})
        )
        await hass.async_block_till_done()
        assert any(
            entity.unique_id.endswith("_fuite")
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not coordinator._listeners
