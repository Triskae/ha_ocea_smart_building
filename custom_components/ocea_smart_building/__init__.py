"""The Ocea Smart Building integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant

from .api import OceaApiClient
from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .history import async_import_history

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
_HISTORY_IMPORTED = "history_imported_v1"


async def _async_import_history_once(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: OceaDataUpdateCoordinator
) -> None:
    """Import daily history once; leave it retryable when the import fails."""
    try:
        rows = await async_import_history(hass, entry, coordinator.client)
    except Exception:
        _LOGGER.exception("Ocea daily history import failed")
        return
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, _HISTORY_IMPORTED: True}
    )
    _LOGGER.info("Ocea daily history import completed with %d rows", rows)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ocea Smart Building from a config entry."""
    client = OceaApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        local_id=entry.data[CONF_LOCAL_ID],
    )

    coordinator = OceaDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not entry.options.get(_HISTORY_IMPORTED):
        entry.async_create_background_task(
            hass,
            _async_import_history_once(hass, entry, coordinator),
            name="ocea_import_daily_history",
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.client.close()
    return unload_ok
