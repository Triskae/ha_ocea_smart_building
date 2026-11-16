"""The Ocea Smart Building integration."""

from __future__ import annotations

import logging

from aiohttp import CookieJar
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_LOCAL_ID, DOMAIN, UA
from .coordinator import OceaConfigEntry, OceaDataUpdateCoordinator
from .history import OceaHistorySynchronizer, history_store
from .ocea import OceaApiClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Use the dwelling rather than the account as the entry's identity."""
    if entry.version != 1:
        return False
    local_id = entry.data.get(CONF_LOCAL_ID)
    if not isinstance(local_id, str) or not local_id.strip():
        _LOGGER.error("Cannot migrate Ocea entry: dwelling identifier is missing")
        return False
    existing = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, local_id)
    if existing is not None and existing.entry_id != entry.entry_id:
        _LOGGER.error(
            "Cannot migrate Ocea entry: another entry already uses this dwelling; "
            "remove the duplicate entry after reviewing its configuration"
        )
        return False
    hass.config_entries.async_update_entry(entry, unique_id=local_id, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OceaConfigEntry) -> bool:
    """Set up Ocea Smart Building from a config entry."""
    session = async_create_clientsession(
        hass,
        auto_cleanup=False,
        connector_owner=False,
        cookie_jar=CookieJar(quote_cookie=False),
    )
    client = OceaApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        user_agent=UA,
        session=session,
    )

    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        history = OceaHistorySynchronizer(hass, entry, client, coordinator)
        entry.async_on_unload(coordinator.async_add_listener(history.async_schedule))
        history.async_schedule()
    except BaseException:
        session.detach()
        raise
    entry.async_on_unload(session.detach)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OceaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove private cached readings, leaving recorder statistics intact."""
    await history_store(hass, entry).async_remove()
