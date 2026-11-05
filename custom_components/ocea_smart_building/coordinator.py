"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from requests import RequestException

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, WATER_FLUID_SENSOR_KEYS
from .models import OceaData, OceaMeter, OceaMeterReading

_LOGGER = logging.getLogger(__name__)


def _current_month_payload(today: date) -> dict[str, str]:
    """Build the daily consumption request for the current local month."""
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(meter: OceaMeter, response: dict[str, Any]) -> OceaMeterReading:
    """Aggregate one PDS response while preserving its metadata."""
    if not isinstance(response, dict):
        raise OceaApiError("Invalid meter consumption response")

    consumptions = response.get("consommations", [])
    if not isinstance(consumptions, list):
        raise OceaApiError("Invalid meter consumption list")

    total = Decimal(0)
    latest_date: str | None = None
    latest_item: dict[str, Any] | None = None
    for item in consumptions:
        if not isinstance(item, dict):
            raise OceaApiError("Invalid meter consumption item")
        try:
            value = Decimal(str(item.get("valeur", 0)).replace(",", "."))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise OceaApiError("Invalid meter consumption value") from err
        if not value.is_finite():
            raise OceaApiError("Invalid meter consumption value")
        total += value
        item_date = str(item.get("date", ""))
        if latest_date is None or item_date >= latest_date:
            latest_date = item_date
            latest_item = item
    latest_item = latest_item or {}
    estimated_leak = latest_item.get("fuiteEstimee")
    if estimated_leak is not None:
        try:
            leak_value = Decimal(str(estimated_leak).replace(",", "."))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise OceaApiError("Invalid estimated leak value") from err
        if not leak_value.is_finite():
            raise OceaApiError("Invalid estimated leak value")
        estimated_leak = float(leak_value)

    return OceaMeterReading(
        meter=meter,
        value=float(total),
        latest_date=latest_date,
        unit=response.get("unite"),
        estimated_leak=estimated_leak,
        consent=latest_item.get("consentement"),
        response_type=latest_item.get("type"),
    )


def _as_update_error(err: Exception) -> Exception:
    """Translate a client error into the matching coordinator exception."""
    if isinstance(err, OceaAuthError):
        return ConfigEntryAuthFailed(f"Authentication failed: {err}")
    if isinstance(err, OceaApiError):
        return UpdateFailed(f"Error fetching data: {err}")
    return UpdateFailed(f"Unexpected error: {err}")


class OceaDataUpdateCoordinator(DataUpdateCoordinator[OceaData]):
    """Manage fetching Ocea consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OceaApiClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self._meters: dict[str, OceaMeter] = {}

    async def _async_setup(self) -> None:
        """Discover the dwelling's water meters once, before the first refresh."""
        try:
            self._meters = await self.hass.async_add_executor_job(
                self._discover_meters
            )
        except Exception as err:
            raise _as_update_error(err) from err

    async def _async_update_data(self) -> OceaData:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
            payload = _current_month_payload(dt_util.now().date())
            meters = await self.hass.async_add_executor_job(
                self._fetch_readings, payload
            )
        except Exception as err:
            raise _as_update_error(err) from err

        data = OceaData(totals=self._aggregate_totals(raw_data), meters=meters)
        _LOGGER.debug(
            "Ocea consumption data updated: %d aggregate values, %d individual meters",
            len(data.totals),
            len(data.meters),
        )
        return data

    def _discover_meters(self) -> dict[str, OceaMeter]:
        """Discover the configured dwelling's water meters (blocking I/O)."""
        pds_data = [
            item
            for item in self.client.get_pds()
            if item.get("id") and item.get("fluide") in WATER_FLUID_SENSOR_KEYS
        ]
        appareils = self.client.get_appareils([str(item["id"]) for item in pds_data])
        appareils_by_pds = {
            str(item["pdsId"]): item for item in appareils if item.get("pdsId")
        }
        return {
            str(item["id"]): OceaMeter.from_api(
                item, appareils_by_pds.get(str(item["id"]))
            )
            for item in pds_data
        }

    def _fetch_readings(self, payload: dict[str, str]) -> dict[str, OceaMeterReading]:
        """Fetch current-month consumption for each meter (blocking I/O)."""
        readings = {
            pds_id: OceaMeterReading(meter=meter, value=None, latest_date=None)
            for pds_id, meter in self._meters.items()
        }
        for pds_id, meter in self._meters.items():
            try:
                response = self.client.get_pds_consumption(pds_id, payload)
                readings[pds_id] = _build_meter_data(meter, response)
            except OceaAuthError:
                raise
            except (OceaApiError, RequestException):
                _LOGGER.warning("Unable to update one water meter")
        return readings

    @staticmethod
    def _aggregate_totals(raw_data: list[dict[str, str]]) -> dict[str, float]:
        """Map the dwelling's dashboard consumption entries to sensor keys."""
        totals: dict[str, float] = {}
        for item in raw_data:
            fluide = item.get("fluide", "")
            # Ocea uses comma as decimal separator
            valeur = float(item.get("valeur", "0").replace(",", "."))
            totals[WATER_FLUID_SENSOR_KEYS.get(fluide, fluide.lower())] = valeur
        return totals
