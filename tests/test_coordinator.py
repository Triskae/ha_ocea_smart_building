"""Tests for the Ocea Smart Building data update coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ocea_smart_building.const import DEFAULT_SCAN_INTERVAL
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_coordinator_uses_default_scan_interval_when_not_given(
    hass: HomeAssistant,
) -> None:
    """Test the coordinator falls back to the default scan interval."""
    coordinator = OceaDataUpdateCoordinator(hass, MagicMock())

    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_coordinator_uses_given_scan_interval(hass: HomeAssistant) -> None:
    """Test the coordinator honors a custom scan interval."""
    coordinator = OceaDataUpdateCoordinator(
        hass, MagicMock(), scan_interval_seconds=3600
    )

    assert coordinator.update_interval == timedelta(seconds=3600)
