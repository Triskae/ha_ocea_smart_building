"""Provide info to system health."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import API_BASE, DOMAIN
from .coordinator import OceaDataUpdateCoordinator


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get info for the info page."""
    info: dict[str, Any] = {
        "can_reach_server": system_health.async_check_can_reach_url(hass, API_BASE),
    }

    entries = hass.config_entries.async_entries(DOMAIN)
    domain_data: dict[str, OceaDataUpdateCoordinator] = hass.data.get(DOMAIN, {})
    # Only the first loaded config entry is reflected; HA's system health
    # page shows a single info dict per domain, not one per dwelling.
    for entry in entries:
        coordinator = domain_data.get(entry.entry_id)
        if coordinator is not None:
            info["last_update_success"] = coordinator.last_update_success
            break

    return info
