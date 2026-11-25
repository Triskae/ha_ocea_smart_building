"""DataUpdateCoordinator for OCEA Smart Building."""

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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PDS_FLUID_METADATA,
)
from .ocea import OCEAApiClient, OCEAApiError, OCEAAuthError, OCEAMeter
from .ocea.const import OCEA_TIME_ZONE
from .ocea.models import (
    OCEAConsumptionDataItem,
    OCEAConsumptionRequest,
    OCEAConsumptionSeries,
    OCEADashboardData,
    OCEADevicesRequest,
    OCEAUnclassifiedConsumptionData,
    OCEAWaterConsumptionData,
)


@dataclass(frozen=True, slots=True)
class OCEADailyConsumption:
    """One dated OCEA consumption value."""

    day: date
    value: Decimal
    consent: bool | None = None


@dataclass(frozen=True, slots=True)
class OCEAMeterReading:
    """Current-month consumption for one OCEA meter."""

    meter: OCEAMeter
    value: Decimal | None
    latest_date: date | datetime | None
    unit: str | None = None
    estimated_leak: Decimal | None = None
    consent: bool | None = None
    response_type: str | None = None
    daily_consumptions: tuple[OCEADailyConsumption, ...] | None = None


@dataclass(frozen=True, slots=True)
class OCEAData:
    """Coordinator payload: dwelling totals and per-meter readings."""

    totals: dict[str, Decimal]
    meters: dict[str, OCEAMeterReading]


_LOGGER = logging.getLogger(__name__)


def daily_consumptions_from_series(
    response: OCEAConsumptionSeries,
) -> tuple[OCEADailyConsumption, ...] | None:
    """Convert complete typed readings to OCEA calendar days."""
    if "consommations" not in response.raw:
        return None
    readings: list[OCEADailyConsumption] = []
    for item in response.items:
        if item.date is None or item.value is None:
            return None
        day = (
            item.date.astimezone(OCEA_TIME_ZONE).date()
            if isinstance(item.date, datetime)
            else item.date
        )
        readings.append(OCEADailyConsumption(day, item.value, item.consent))
    return tuple(readings)


def _current_month_payload(now: datetime) -> OCEAConsumptionRequest:
    """Build the daily consumption request for the current local month."""
    today = now.astimezone(OCEA_TIME_ZONE).date()
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(meter: OCEAMeter, response: OCEAConsumptionSeries) -> OCEAMeterReading:
    """Aggregate one PDS response while preserving its metadata."""
    total = Decimal(0)
    valid_items: list[tuple[date, OCEAConsumptionDataItem]] = []
    dated_items: list[tuple[date, OCEAConsumptionDataItem]] = []
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
        return OCEAMeterReading(meter=meter, value=None, latest_date=None, unit=response.unit)

    latest_date: date | datetime | None = None
    latest_item: OCEAConsumptionDataItem | None = None
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
        if isinstance(latest_item, (OCEAWaterConsumptionData, OCEAUnclassifiedConsumptionData))
        else None
    )
    return OCEAMeterReading(
        meter=meter,
        value=total,
        latest_date=latest_date,
        unit=response.unit,
        estimated_leak=estimated_leak,
        consent=latest_item.consent if latest_item is not None else None,
        response_type=latest_item.response_type if latest_item is not None else None,
        daily_consumptions=daily_consumptions_from_series(response),
    )


def _as_update_error(err: Exception) -> Exception:
    """Translate a client error into the matching coordinator exception."""
    if isinstance(err, OCEAAuthError):
        return ConfigEntryAuthFailed(f"Authentication failed: {err}")
    if isinstance(err, OCEAApiError):
        return UpdateFailed(f"Error fetching data: {err}")
    if isinstance(err, (ClientError, TimeoutError)):
        return UpdateFailed("Error connecting to OCEA")
    return UpdateFailed("Unexpected error fetching data")


class OCEADataUpdateCoordinator(DataUpdateCoordinator[OCEAData]):
    """Manage fetching OCEA consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OCEAApiClient,
        config_entry: OCEAConfigEntry,
        local_id: str,
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
        self.local_id = local_id
        self._meters: dict[str, OCEAMeter] = {}

    async def _async_setup(self) -> None:
        """Discover supported dwelling meters once, before the first refresh."""
        try:
            self._meters = await self._discover_meters()
        except Exception as err:
            raise _as_update_error(err) from err

    async def _async_update_data(self) -> OCEAData:
        """Fetch data from the OCEA API."""
        _LOGGER.debug("OCEA coordinator: starting data fetch")
        try:
            raw_data = await self.client.get_consumptions(self.local_id)
            payload = _current_month_payload(dt_util.now())
            meters = await self._fetch_readings(payload)
        except Exception as err:
            raise _as_update_error(err) from err

        data = OCEAData(totals=self._aggregate_totals(raw_data), meters=meters)
        _LOGGER.debug(
            "OCEA consumption data updated: %d aggregate values, %d individual meters",
            len(data.totals),
            len(data.meters),
        )
        return data

    async def _discover_meters(self) -> dict[str, OCEAMeter]:
        """Discover the configured dwelling's supported meters."""
        pds_data = [
            item
            for item in await self.client.get_pds(self.local_id)
            if item.id and item.fluid is not None and item.fluid in PDS_FLUID_METADATA
        ]
        payload: OCEADevicesRequest = {"pdsIds": [str(item.id) for item in pds_data]}
        appareils = await self.client.get_appareils(payload)
        appareils_by_pds = {
            str(item.pds_id): item for item in appareils if item.pds_id
        }
        return {
            str(item.id): item.as_meter(appareils_by_pds.get(str(item.id)))
            for item in pds_data
        }

    async def _fetch_readings(self, payload: OCEAConsumptionRequest) -> dict[str, OCEAMeterReading]:
        """Fetch current-month consumption for each meter."""
        readings = {
            pds_id: OCEAMeterReading(meter=meter, value=None, latest_date=None)
            for pds_id, meter in self._meters.items()
        }
        for pds_id, meter in self._meters.items():
            try:
                response = await self.client.get_pds_consumption(self.local_id, pds_id, payload)
                readings[pds_id] = _build_meter_data(meter, response)
            except OCEAAuthError:
                raise
            except (OCEAApiError, ClientError, TimeoutError):
                _LOGGER.warning("Unable to update one OCEA meter")
        return readings

    @staticmethod
    def _aggregate_totals(raw_data: list[OCEADashboardData]) -> dict[str, Decimal]:
        """Map the dwelling's dashboard consumption entries to sensor keys."""
        totals: dict[str, Decimal] = {}
        for item in raw_data:
            fluid_metadata = PDS_FLUID_METADATA.get(item.fluid)
            if fluid_metadata is None:
                _LOGGER.warning("Ignoring unsupported dashboard fluid")
                continue
            if item.value is None:
                _LOGGER.warning("Ignoring invalid dashboard consumption value")
                continue
            totals[fluid_metadata.dashboard_sensor_key] = item.value
        return totals


OCEAConfigEntry = ConfigEntry[OCEADataUpdateCoordinator]
