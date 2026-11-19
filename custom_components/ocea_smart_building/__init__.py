"""The Ocea Smart Building integration."""

from __future__ import annotations

import logging

from aiohttp import CookieJar
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import UA
from .coordinator import OceaConfigEntry, OceaDataUpdateCoordinator
from .ocea import OceaApiClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


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
    except BaseException:
        session.detach()
        raise
    entry.async_on_unload(session.detach)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OceaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
