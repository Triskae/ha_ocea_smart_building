"""The Ocea Smart Building integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_LOCAL_ID, DOMAIN, UA
from .coordinator import OceaDataUpdateCoordinator
from .ocea import OceaApiClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ocea Smart Building from a config entry."""
    session = async_create_clientsession(hass, auto_cleanup=False, connector_owner=False)
    client = OceaApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        local_id=entry.data[CONF_LOCAL_ID],
        user_agent=UA,
        session=session,
    )

    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        session.detach()
        raise
    entry.async_on_unload(session.detach)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
