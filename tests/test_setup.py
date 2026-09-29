"""Tests for Ocea Smart Building entry setup (scan interval wiring)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import (
    CONF_LOCAL_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

EMAIL = "resident@example.com"
PASSWORD = "stored-secret"
LOCAL_ID = "local-123"


def _make_entry(hass: HomeAssistant, **options: int) -> MockConfigEntry:
    """Create and register a config entry with optional scan interval options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ocea - Alice",
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_LOCAL_ID: LOCAL_ID,
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_uses_default_scan_interval_without_options(
    hass: HomeAssistant,
) -> None:
    """Test the coordinator uses the default scan interval without options."""
    entry = _make_entry(hass)
    client = MagicMock()
    client.get_consumptions.return_value = []

    with patch(
        "custom_components.ocea_smart_building.OceaApiClient", return_value=client
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_uses_configured_scan_interval(hass: HomeAssistant) -> None:
    """Test the coordinator honors the scan interval stored in options."""
    entry = _make_entry(hass, **{CONF_SCAN_INTERVAL: 3600})
    client = MagicMock()
    client.get_consumptions.return_value = []

    with patch(
        "custom_components.ocea_smart_building.OceaApiClient", return_value=client
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval == timedelta(seconds=3600)
