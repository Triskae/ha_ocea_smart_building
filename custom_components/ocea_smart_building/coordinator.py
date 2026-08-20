"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OceaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage fetching Ocea consumption data."""

    def __init__(self, hass: HomeAssistant, client: OceaApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
            precise_data = await self.hass.async_add_executor_job(
                self.client.get_precise_consumptions
            )
        except OceaAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except OceaApiError as err:
            if err.status_code == 403:
                raise ConfigEntryAuthFailed(
                    "Ocea no longer authorizes the configured dwelling; "
                    "reauthentication will select an accessible occupation"
                ) from err
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        result: dict[str, Any] = {}
        for item in [*raw_data, *precise_data]:
            fluide = item.get("fluide", "")
            valeur_str = item.get("valeur", "0")
            # Ocea uses comma as decimal separator
            valeur = float(valeur_str.replace(",", "."))

            if fluide == "EauFroide":
                data_key = "eau_froide"
            elif fluide == "EauChaude":
                data_key = "eau_chaude"
            else:
                data_key = fluide.lower()
            if item.get("leak_estimate"):
                data_key = f"fuite_{data_key}"
                result[f"{data_key}_date"] = item.get("date", "")
            elif item.get("precise_daily"):
                data_key = f"{data_key}_dernier_jour_precis"
                result[f"{data_key}_date"] = item.get("date", "")
            elif item.get("precise"):
                data_key = f"{data_key}_precise"
            result[data_key] = valeur

        _LOGGER.debug("Ocea consumption data updated: %s", result)
        return result
