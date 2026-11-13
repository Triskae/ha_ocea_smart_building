"""Provide info to system health."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator import OceaConfigEntry
from .ocea.const import API_BASE

SystemHealthValue = int | bool | Awaitable[str | dict[str, str]]


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, SystemHealthValue]:
    """Get info for the info page."""
    info: dict[str, SystemHealthValue] = {
        "can_reach_server": system_health.async_check_can_reach_url(hass, API_BASE),
    }

    entries: list[OceaConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    if entries:
        coordinators = [entry.runtime_data for entry in entries]
        info["last_update_success"] = all(
            coordinator.last_update_success for coordinator in coordinators
        )

        data_coordinators = [
            coordinator for coordinator in coordinators if coordinator.data is not None
        ]
        if data_coordinators:
            meter_count = sum(len(coordinator.data.meters) for coordinator in data_coordinators)
            available_readings = sum(
                reading.value is not None
                for coordinator in data_coordinators
                if coordinator.last_update_success
                for reading in coordinator.data.meters.values()
            )
            info["meter_count"] = meter_count
            info["meter_readings_available"] = available_readings
            info["meter_readings_unavailable"] = meter_count - available_readings

    return info
