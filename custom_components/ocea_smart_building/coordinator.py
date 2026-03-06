"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OceaApiClient, OceaAuthError, OceaApiError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OceaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Manage fetching Ocea water consumption data."""

    def __init__(self, hass: HomeAssistant, client: OceaApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, float]:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
        except OceaAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except OceaApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        result: dict[str, float] = {}
        for item in raw_data:
            fluide = item.get("fluide", "")
            valeur_str = item.get("valeur", "0")
            # Ocea uses comma as decimal separator
            valeur = float(valeur_str.replace(",", "."))

            if fluide == "EauFroide":
                result["eau_froide"] = valeur
            elif fluide == "EauChaude":
                result["eau_chaude"] = valeur
            else:
                result[fluide.lower()] = valeur

        _LOGGER.debug("Ocea consumption data updated: %s", result)
        return result
