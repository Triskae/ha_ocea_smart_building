"""Tests for the Ocea Smart Building system health platform."""

from __future__ import annotations

from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import DOMAIN
from custom_components.ocea_smart_building.system_health import (
    async_register,
    system_health_info,
)


def test_async_register_registers_info_callback() -> None:
    """Test the info callback is registered with system health."""
    register = Mock()

    async_register(Mock(), register)

    register.async_register_info.assert_called_once_with(system_health_info)


async def test_system_health_info_without_loaded_entry(hass: HomeAssistant) -> None:
    """Test the info dict omits update status when no entry is loaded."""
    with patch(
        "custom_components.ocea_smart_building.system_health.system_health"
        ".async_check_can_reach_url",
        return_value="ok",
    ) as mock_check:
        info = await system_health_info(hass)

    assert set(info) == {"can_reach_server"}
    assert await info["can_reach_server"] == "ok"
    mock_check.assert_called_once()


async def test_system_health_info_reports_last_update_success(
    hass: HomeAssistant,
) -> None:
    """Test the info dict reflects the loaded coordinator's update status."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = Mock(last_update_success=True)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    with patch(
        "custom_components.ocea_smart_building.system_health.system_health"
        ".async_check_can_reach_url",
        return_value="ok",
    ):
        info = await system_health_info(hass)

    assert info["last_update_success"] is True
    assert await info["can_reach_server"] == "ok"
