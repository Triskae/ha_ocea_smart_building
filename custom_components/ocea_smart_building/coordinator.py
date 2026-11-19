"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOCAL_ID,
    DASHBOARD_FLUID_SENSOR_KEYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    WATER_FLUID_SENSOR_KEYS,
)
from .ocea import OceaApiClient, OceaApiError, OceaAuthError, OceaMeter
from .ocea.const import OCEA_TIME_ZONE
from .ocea.models import (
    OceaConsumptionDataItem,
    OceaConsumptionRequest,
    OceaConsumptionSeries,
    OceaDashboardData,
    OceaDevicesRequest,
    OceaUnclassifiedConsumptionData,
    OceaWaterConsumptionData,
)


@dataclass(frozen=True, slots=True)
class OceaMeterReading:
    """Current-month consumption for one Ocea meter."""

    meter: OceaMeter
    value: Decimal | None
    latest_date: date | datetime | None
    unit: str | None = None
    estimated_leak: Decimal | None = None
    consent: bool | None = None
    response_type: str | None = None


@dataclass(frozen=True, slots=True)
class OceaData:
    """Coordinator payload: dwelling totals and per-meter readings."""

    totals: dict[str, Decimal]
    meters: dict[str, OceaMeterReading]


_LOGGER = logging.getLogger(__name__)


def _current_month_payload(today: date) -> OceaConsumptionRequest:
    """Build the daily consumption request for the current local month."""
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(meter: OceaMeter, response: OceaConsumptionSeries) -> OceaMeterReading:
    """Aggregate one PDS response while preserving its metadata."""
    total = Decimal(0)
    valid_items: list[tuple[date, OceaConsumptionDataItem]] = []
    dated_items: list[tuple[date, OceaConsumptionDataItem]] = []
    unknown_date = False
    for item in response.items:
        if item.date is None:
            unknown_date = True
            continue
        day = (
            item.date.astimezone(OCEA_TIME_ZONE).date()
            if isinstance(item.date, datetime)
            else item.date
        )
        dated_items.append((day, item))
        if item.value is None:
            continue
        total += item.value
        valid_items.append((day, item))
    if len(valid_items) != len(response.items):
        _LOGGER.warning("Some meter readings were invalid; the consumption total may be incomplete")
    if response.items and not valid_items:
        return OceaMeterReading(meter=meter, value=None, latest_date=None, unit=response.unit)

    latest_date: date | datetime | None = None
    latest_item: OceaConsumptionDataItem | None = None
    if unknown_date:
        _LOGGER.warning("Some meter reading dates are invalid; latest metadata is unavailable")
    elif dated_items:
        latest_day = max(day for day, _ in dated_items)
        candidates = [item for day, item in dated_items if day == latest_day]
        instants = [(item.date, item) for item in candidates if isinstance(item.date, datetime)]
        if instants and len(instants) != len(candidates):
            _LOGGER.warning("The latest meter reading date is ambiguous; metadata is unavailable")
            latest_date = latest_day
        elif instants:
            _, latest_item = max(
                enumerate(instants),
                key=lambda pair: (pair[1][0].astimezone(timezone.utc), pair[0]),
            )[1]
            latest_date = latest_item.date
        else:
            latest_item = candidates[-1]
            latest_date = latest_item.date
        if latest_item is not None and latest_item.value is None:
            _LOGGER.warning("The latest meter reading is invalid; metadata is unavailable")
            latest_item = None
    estimated_leak = (
        latest_item.estimated_leak
        if isinstance(latest_item, (OceaWaterConsumptionData, OceaUnclassifiedConsumptionData))
        else None
    )
    return OceaMeterReading(
        meter=meter,
        value=total,
        latest_date=latest_date,
        unit=response.unit,
        estimated_leak=estimated_leak,
        consent=latest_item.consent if latest_item is not None else None,
        response_type=latest_item.response_type if latest_item is not None else None,
    )


def _as_update_error(err: Exception) -> Exception:
    """Translate a client error into the matching coordinator exception."""
    if isinstance(err, OceaAuthError):
        return ConfigEntryAuthFailed(f"Authentication failed: {err}")
    if isinstance(err, OceaApiError):
        return UpdateFailed(f"Error fetching data: {err}")
    if isinstance(err, (ClientError, TimeoutError)):
        return UpdateFailed("Error connecting to Ocea")
    return UpdateFailed("Unexpected error fetching data")


class OceaDataUpdateCoordinator(DataUpdateCoordinator[OceaData]):
    """Manage fetching Ocea consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OceaApiClient,
        config_entry: OceaConfigEntry,
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
        self._local_id: str = config_entry.data[CONF_LOCAL_ID]
        self._meters: dict[str, OceaMeter] = {}

    async def _async_setup(self) -> None:
        """Discover the dwelling's water meters once, before the first refresh."""
        try:
            self._meters = await self._discover_meters()
        except Exception as err:
            raise _as_update_error(err) from err

    async def _async_update_data(self) -> OceaData:
        """Fetch data from the Ocea API."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.client.get_consumptions(self._local_id)
            payload = _current_month_payload(dt_util.now().date())
            meters = await self._fetch_readings(payload)
        except Exception as err:
            raise _as_update_error(err) from err

        data = OceaData(totals=self._aggregate_totals(raw_data), meters=meters)
        _LOGGER.debug(
            "Ocea consumption data updated: %d aggregate values, %d individual meters",
            len(data.totals),
            len(data.meters),
        )
        return data

    async def _discover_meters(self) -> dict[str, OceaMeter]:
        """Discover the configured dwelling's water meters."""
        pds_data = [
            item
            for item in await self.client.get_pds(self._local_id)
            if item.id and item.fluid in WATER_FLUID_SENSOR_KEYS
        ]
        payload: OceaDevicesRequest = {"pdsIds": [str(item.id) for item in pds_data]}
        appareils = await self.client.get_appareils(payload)
        appareils_by_pds = {
            str(item.pds_id): item for item in appareils if item.pds_id
        }
        return {
            str(item.id): item.as_meter(appareils_by_pds.get(str(item.id)))
            for item in pds_data
        }

    async def _fetch_readings(self, payload: OceaConsumptionRequest) -> dict[str, OceaMeterReading]:
        """Fetch current-month consumption for each meter."""
        readings = {
            pds_id: OceaMeterReading(meter=meter, value=None, latest_date=None)
            for pds_id, meter in self._meters.items()
        }
        for pds_id, meter in self._meters.items():
            try:
                response = await self.client.get_pds_consumption(self._local_id, pds_id, payload)
                readings[pds_id] = _build_meter_data(meter, response)
            except OceaAuthError:
                raise
            except (OceaApiError, ClientError, TimeoutError):
                _LOGGER.warning("Unable to update one water meter")
        return readings

    @staticmethod
    def _aggregate_totals(raw_data: list[OceaDashboardData]) -> dict[str, Decimal]:
        """Map the dwelling's dashboard consumption entries to sensor keys."""
        totals: dict[str, Decimal] = {}
        for item in raw_data:
            key = DASHBOARD_FLUID_SENSOR_KEYS.get(item.fluid)
            if key is None:
                _LOGGER.warning("Ignoring unsupported dashboard fluid")
                continue
            if item.value is None:
                _LOGGER.warning("Ignoring invalid dashboard consumption value")
                continue
            totals[key] = item.value
        return totals


OceaConfigEntry = ConfigEntry[OceaDataUpdateCoordinator]
