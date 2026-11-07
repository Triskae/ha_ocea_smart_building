"""Diagnostics support for Ocea Smart Building."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator

# Dwelling identifier is redacted too: AGENTS.md treats it as sensitive.
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_LOCAL_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "entry_data": async_redact_data(entry.data, TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "data": coordinator.data,
    }
