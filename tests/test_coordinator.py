"""Tests for the Ocea Smart Building data update coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import DEFAULT_SCAN_INTERVAL, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_coordinator_uses_default_scan_interval_when_not_given(
    hass: HomeAssistant,
) -> None:
    """Test the coordinator falls back to the default scan interval."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, MagicMock(), entry)

    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_coordinator_uses_given_scan_interval(hass: HomeAssistant) -> None:
    """Test the coordinator honors a custom scan interval."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(
        hass, MagicMock(), entry, scan_interval_seconds=3600
    )

    assert coordinator.update_interval == timedelta(seconds=3600)
