"""Synchronize dated Ocea consumption as external statistics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Final
from uuid import uuid4

from aiohttp import ClientError
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOCAL_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PDS_FLUID_METADATA,
)
from .coordinator import (
    OceaDailyConsumption,
    OceaDataUpdateCoordinator,
    OceaMeterReading,
    daily_consumptions_from_series,
)
from .ocea import OceaApiClient, OceaApiError, OceaAuthError, OceaMeter, OceaResident
from .ocea.const import OCEA_TIME_ZONE
from .ocea.models import OceaConsumptionRequest, OceaConsumptionSeries

_LOGGER = logging.getLogger(__name__)
_CHUNK_DAYS: Final = 30
_RESTORE_STATISTICS_LIMIT: Final = 100_000


def history_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, object]]:
    """Create the private store for an entry's historical readings."""
    return Store(
        hass, 1, f"{DOMAIN}_history.{entry.entry_id}", private=True, atomic_writes=True
    )


@dataclass(slots=True)
class MeterHistory:
    """Persisted consumption and next unprocessed date for a single meter."""

    start: date
    cursor: date
    days: dict[date, Decimal] = field(default_factory=dict)
    next_request: datetime | None = None
    recheck_cursor: date | None = None


def _api_date(value: object) -> date:
    """Parse an Ocea calendar date without exposing the source value."""
    if not isinstance(value, str):
        raise OceaApiError("Missing or invalid Ocea date")
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value).date()
    except ValueError as err:
        raise OceaApiError("Missing or invalid Ocea date") from err


def _occupation_start(
    resident: OceaResident, local_id: str, today: date
) -> date:
    """Find the unique current occupation for the configured dwelling."""
    matching: list[date] = []
    for dwelling in resident.dwellings:
        if dwelling.local_id != local_id:
            continue
        for occupation in dwelling.occupations:
            if occupation.start is None or (
                occupation.end is None and not occupation.open_ended
            ):
                continue
            start = occupation.start.date()
            end = occupation.end.date() if occupation.end is not None else None
            if start <= today and (end is None or end >= today):
                matching.append(start)
    if len(matching) != 1:
        raise OceaApiError("No unique active Ocea occupation for the dwelling")
    return matching[0]


def _history_start(occupation_start: date, meter: OceaMeter) -> date:
    """Bound a meter's history by both occupation and device installation."""
    return max(occupation_start, _api_date(meter.installation_date))


def _history_id(
    local_id: str,
    meter: OceaMeter,
) -> str:
    """Build a stable cache key from the dwelling, measurement point and fluid."""
    identity = f"{local_id}\0{meter.identifier}\0{meter.pds.fluid}"
    return sha256(identity.encode()).hexdigest()


def _meter_statistic_id(local_id: str, meter: OceaMeter) -> str:
    """Return a stable Recorder ID without exposing Ocea identifiers."""
    key = _history_id(local_id, meter)
    return f"{DOMAIN}:{PDS_FLUID_METADATA[meter.pds.fluid].statistic_id_prefix}_{key}"


def _dwelling_history_id(local_id: str, fluid: str) -> str:
    """Build a stable, separate cache key for one dwelling and fluid."""
    return sha256(f"{local_id}\0{fluid}".encode()).hexdigest()


def _dwelling_statistic_id(local_id: str, fluid: str) -> str:
    """Return a stable Recorder ID for a dwelling consumption series."""
    prefix = PDS_FLUID_METADATA[fluid].statistic_id_prefix
    return f"{DOMAIN}:{prefix}_{_dwelling_history_id(local_id, fluid)}"


def _meter_metadata(local_id: str, meter: OceaMeter) -> StatisticMetaData:
    """Build Recorder metadata for an existing PDS series."""
    fluid_metadata = PDS_FLUID_METADATA[meter.pds.fluid]
    serial_number = meter.serial_number or _history_id(local_id, meter)[:8]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"OCEA - {local_id} - {fluid_metadata.statistic_display_label} - {serial_number}",
        source=DOMAIN,
        statistic_id=_meter_statistic_id(local_id, meter),
        unit_class=fluid_metadata.statistic_unit_class,
        unit_of_measurement=fluid_metadata.statistic_unit_of_measurement,
    )


def _dwelling_metadata(local_id: str, fluid: str) -> StatisticMetaData:
    """Build Recorder metadata for a dwelling consumption series."""
    fluid_metadata = PDS_FLUID_METADATA[fluid]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"OCEA - {local_id} - {fluid_metadata.statistic_display_label}",
        source=DOMAIN,
        statistic_id=_dwelling_statistic_id(local_id, fluid),
        unit_class=fluid_metadata.statistic_unit_class,
        unit_of_measurement=fluid_metadata.statistic_unit_of_measurement,
    )


def _decimal_value(value: object) -> Decimal:
    """Validate a non-negative, finite consumption value."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise OceaApiError("Invalid daily consumption value")
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as err:
        raise OceaApiError("Invalid daily consumption value") from err
    if not result.is_finite() or result < 0:
        raise OceaApiError("Invalid daily consumption value")
    return result


def _parse_daily_response(
    response: OceaConsumptionSeries, expected_unit: str
) -> tuple[OceaDailyConsumption, ...]:
    """Validate a daily response without assuming missing values are zero."""
    if response.unit != expected_unit:
        raise OceaApiError("Unsupported daily consumption unit")
    items = daily_consumptions_from_series(response)
    if items is None:
        raise OceaApiError("Invalid daily consumption list")
    readings: list[OceaDailyConsumption] = []
    seen: set[date] = set()
    for item in items:
        if item.consent is False:
            raise OceaApiError("Ocea does not provide consent for daily consumption")
        if item.day in seen:
            raise OceaApiError("Duplicate daily consumption date")
        if not item.value.is_finite() or item.value < 0:
            raise OceaApiError("Invalid daily consumption value")
        seen.add(item.day)
        readings.append(item)
    return tuple(readings)


def _current_daily_readings(
    reading: OceaMeterReading, expected_unit: str, today: date
) -> tuple[OceaDailyConsumption, ...]:
    """Validate coordinator readings before reusing them for history."""
    if reading.unit != expected_unit or reading.daily_consumptions is None:
        raise OceaApiError("Invalid current daily consumption unit or readings")
    seen: set[date] = set()
    for item in reading.daily_consumptions:
        if (
            item.consent is False
            or item.day in seen
            or not today.replace(day=1) <= item.day <= today
            or not item.value.is_finite()
            or item.value < 0
        ):
            raise OceaApiError("Invalid current daily consumption")
        seen.add(item.day)
    return reading.daily_consumptions


def _load_histories(data: dict[str, object] | None) -> tuple[str, dict[str, MeterHistory]]:
    """Validate persisted history before using it."""
    if data is None:
        return uuid4().hex, {}
    generation = data.get("generation")
    meters = data.get("meters")
    if not isinstance(generation, str) or not generation or not isinstance(meters, dict):
        raise OceaApiError("Invalid persisted consumption history")
    histories: dict[str, MeterHistory] = {}
    for key, raw in meters.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise OceaApiError("Invalid persisted consumption history")
        start = _api_date(raw.get("start"))
        cursor = _api_date(raw.get("cursor"))
        raw_next_request = raw.get("next_request")
        next_request: datetime | None = None
        if raw_next_request is not None:
            if not isinstance(raw_next_request, str):
                raise OceaApiError("Invalid persisted consumption history")
            try:
                next_request = datetime.fromisoformat(raw_next_request)
            except ValueError as err:
                raise OceaApiError("Invalid persisted consumption history") from err
            if next_request.utcoffset() is None:
                raise OceaApiError("Invalid persisted consumption history")
        recheck_cursor = _api_date(raw.get("recheck_cursor", raw.get("start")))
        raw_days = raw.get("days")
        if cursor < start or recheck_cursor < start or not isinstance(raw_days, dict):
            raise OceaApiError("Invalid persisted consumption history")
        days: dict[date, Decimal] = {}
        for day, value in raw_days.items():
            parsed_day = _api_date(day)
            if parsed_day < start:
                raise OceaApiError("Invalid persisted consumption history")
            days[parsed_day] = _decimal_value(value)
        histories[key] = MeterHistory(start, cursor, days, next_request, recheck_cursor)
    return generation, histories


def _serialize_histories(
    generation: str, histories: dict[str, MeterHistory]
) -> dict[str, object]:
    """Serialize histories without storing account identifiers or credentials."""
    return {
        "generation": generation,
        "meters": {
            key: {
                "start": history.start.isoformat(),
                "cursor": history.cursor.isoformat(),
                "days": {day.isoformat(): str(value) for day, value in history.days.items()},
                "recheck_cursor": (
                    history.recheck_cursor.isoformat()
                    if history.recheck_cursor
                    else history.start.isoformat()
                ),
                "next_request": (
                    history.next_request.isoformat() if history.next_request else None
                ),
            }
            for key, history in histories.items()
        }
    }


def _daily_statistics(
    start_day: date,
    days: dict[date, Decimal],
    changed_since: date | None = None,
) -> list[StatisticData]:
    """Build cumulative hourly statistics, assigning each daily value to its day."""
    if not days:
        return []

    def day_hour(day: date) -> datetime:
        next_midnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=OCEA_TIME_ZONE)
        return (dt_util.as_utc(next_midnight) - timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )

    result: list[StatisticData] = []
    if changed_since is None:
        result.append(
            StatisticData(start=day_hour(start_day) - timedelta(hours=1), state=0, sum=0)
        )
    total = Decimal(0)
    for day, value in sorted(days.items()):
        total += value
        if changed_since is None or day >= changed_since:
            result.append(
                StatisticData(start=day_hour(day), state=float(value), sum=float(total))
            )
    return result


class OceaHistorySynchronizer:
    """Backfill and update external statistics for meters and dwelling fluids."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OceaApiClient,
        coordinator: OceaDataUpdateCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinator = coordinator
        self.local_id: str = entry.data[CONF_LOCAL_ID]
        self.store = history_store(hass, entry)
        self._task: asyncio.Task[None] | None = None
        self._histories: dict[str, MeterHistory] | None = None
        self._generation: str | None = None
        self._published: set[str] = set()

    async def _async_restore_history(
        self, statistic_id: str, start: date, today: date
    ) -> MeterHistory | None:
        """Recover cached daily values from Recorder before an API backfill."""
        statistics = await self.hass.async_add_executor_job(
            get_last_statistics,
            self.hass,
            _RESTORE_STATISTICS_LIMIT,
            statistic_id,
            False,
            {"state", "sum"},
        )
        rows = statistics.get(statistic_id, [])
        if not rows:
            return None

        days: dict[date, Decimal] = {}
        history_start = start
        for row in rows:
            timestamp = row.get("start")
            state = row.get("state")
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or isinstance(state, bool)
                or not isinstance(state, (int, float))
            ):
                continue
            local_start = dt_util.utc_from_timestamp(timestamp).astimezone(OCEA_TIME_ZONE)
            if local_start.hour == 22 and state == 0:
                history_start = min(history_start, local_start.date() + timedelta(days=1))
            elif local_start.hour == 23 and local_start.date() <= today:
                try:
                    days[local_start.date()] = _decimal_value(state)
                except OceaApiError:
                    _LOGGER.warning("Ignoring one invalid restored Ocea statistic value")
                    continue
                history_start = min(history_start, local_start.date())

        if not days:
            return None
        _LOGGER.debug("Restored %d Ocea daily readings from Recorder", len(days))
        return MeterHistory(
            history_start,
            start,
            days,
            recheck_cursor=history_start,
        )

    @callback
    def async_schedule(self) -> None:
        """Schedule one synchronization without overlapping previous cycles."""
        if self._task is not None and not self._task.done():
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_sync(), "Ocea consumption history"
        )

    async def _async_sync(self) -> None:
        """Synchronize each meter and dwelling fluid independently."""
        data = self.coordinator.data
        if data is None:
            return
        try:
            if self._histories is None:
                try:
                    self._generation, self._histories = _load_histories(
                        await self.store.async_load()
                    )
                except OceaApiError as err:
                    _LOGGER.warning(
                        "Invalid cached Ocea history; rebuilding from the API: %s", err
                    )
                    self._generation = uuid4().hex
                    self._histories = {}
                    await self.store.async_save(
                        _serialize_histories(self._generation, self._histories)
                    )
            local_now = dt_util.now().astimezone(OCEA_TIME_ZONE)
            today = local_now.date()
            now = dt_util.as_utc(local_now)
            occupation_start = _occupation_start(
                await self.client.get_resident(), self.local_id, today
            )
        except OceaApiError as err:
            _LOGGER.warning("Unable to synchronize Ocea consumption history: %s", err)
            return
        except (OceaAuthError, ClientError, TimeoutError, OSError):
            _LOGGER.warning("Unable to synchronize Ocea consumption history")
            return

        for reading in data.meters.values():
            try:
                await self._async_sync_meter(reading, occupation_start, today, now)
            except OceaAuthError:
                _LOGGER.warning("Unable to authenticate while synchronizing Ocea history")
                return
            except OceaApiError as err:
                _LOGGER.warning("Unable to synchronize one Ocea meter history: %s", err)
            except (ClientError, TimeoutError, OSError):
                _LOGGER.warning("Unable to synchronize one Ocea meter history")

        try:
            dwelling_fluids = await self.client.get_dwelling_fluids(self.local_id)
        except OceaAuthError:
            _LOGGER.warning("Unable to authenticate while synchronizing Ocea history")
            return
        except OceaApiError as err:
            _LOGGER.warning("Unable to discover Ocea dwelling fluids: %s", err)
            return
        except (ClientError, TimeoutError, OSError):
            _LOGGER.warning("Unable to discover Ocea dwelling fluids")
            return
        unsupported_count = len(set(dwelling_fluids) - PDS_FLUID_METADATA.keys())
        if unsupported_count:
            _LOGGER.debug("Ignoring %d unsupported dwelling fluids", unsupported_count)
        for fluid in sorted(set(dwelling_fluids) & PDS_FLUID_METADATA.keys()):
            try:
                await self._async_sync_series(fluid, occupation_start, today, now, None)
            except OceaAuthError:
                _LOGGER.warning("Unable to authenticate while synchronizing Ocea history")
                return
            except OceaApiError as err:
                _LOGGER.warning("Unable to synchronize one Ocea dwelling history: %s", err)
            except (ClientError, TimeoutError, OSError):
                _LOGGER.warning("Unable to synchronize one Ocea dwelling history")

    async def _async_sync_meter(
        self, reading: OceaMeterReading, occupation_start: date, today: date, now: datetime
    ) -> None:
        """Synchronize one meter."""
        await self._async_sync_series(
            reading.meter.pds.fluid, occupation_start, today, now, reading
        )

    async def _async_sync_series(
        self,
        fluid: str,
        occupation_start: date,
        today: date,
        now: datetime,
        reading: OceaMeterReading | None,
    ) -> None:
        """Update one series and publish recalculated cumulative sums."""
        assert self._histories is not None
        assert self._generation is not None
        fluid_metadata = PDS_FLUID_METADATA.get(fluid)
        if fluid_metadata is None:
            raise OceaApiError("Unsupported consumption fluid")
        expected_unit = fluid_metadata.api_unit
        meter = reading.meter if reading is not None else None
        start = _history_start(occupation_start, meter) if meter else occupation_start
        if start > today:
            raise OceaApiError("Ocea consumption history begins in the future")
        key = (
            _history_id(self.local_id, meter)
            if meter else _dwelling_history_id(self.local_id, fluid)
        )
        metadata = (
            _meter_metadata(self.local_id, meter)
            if meter else _dwelling_metadata(self.local_id, fluid)
        )
        previous = self._histories.get(key)
        if previous is None:
            previous = await self._async_restore_history(metadata["statistic_id"], start, today)
        if previous is None:
            previous = MeterHistory(start, start, recheck_cursor=start)
        history_start = min(start, previous.start)
        cursor = min(previous.cursor, start) if start < previous.start else previous.cursor
        recheck_cursor = previous.recheck_cursor or previous.start
        if start < previous.start:
            recheck_cursor = min(recheck_cursor, start)
        history = MeterHistory(
            history_start,
            cursor,
            previous.days.copy(),
            previous.next_request,
            recheck_cursor,
        )
        current_items = None
        if reading is not None:
            try:
                current_items = _current_daily_readings(reading, expected_unit, today)
            except OceaApiError:
                _LOGGER.warning("Unable to use current daily readings for one Ocea meter")

        rechecking = False
        if history.cursor <= today:
            request_start = history.cursor
            end = min(request_start + timedelta(days=_CHUNK_DAYS - 1), today)
            advance_cursor = True
        elif history.start < today:
            last_completed_day = today - timedelta(days=1)
            request_start = history.recheck_cursor or history.start
            if request_start > last_completed_day:
                request_start = history.start
            end = min(request_start + timedelta(days=_CHUNK_DAYS - 1), last_completed_day)
            advance_cursor = False
            rechecking = True
        else:
            request_start = None
            end = None
            advance_cursor = False

        if current_items is not None:
            for item in current_items:
                if start <= item.day <= today:
                    history.days[item.day] = item.value

        if request_start is not None and end is not None:
            try:
                if (
                    advance_cursor
                    and request_start >= today.replace(day=1)
                    and current_items is not None
                ):
                    items = tuple(
                        item
                        for item in current_items
                        if request_start <= item.day <= end
                    )
                elif history.next_request is None or now >= history.next_request:
                    history.next_request = now + timedelta(seconds=DEFAULT_SCAN_INTERVAL)
                    reserved = MeterHistory(
                        history.start,
                        history.cursor,
                        history.days.copy(),
                        history.next_request,
                        history.recheck_cursor,
                    )
                    self._histories = {**self._histories, key: reserved}
                    await self.store.async_save(
                        _serialize_histories(self._generation, self._histories)
                    )
                    payload: OceaConsumptionRequest = {
                        "debut": f"{request_start.isoformat()}T00:00:00.000Z",
                        "fin": f"{end.isoformat()}T23:59:59.999Z",
                        "granularity": "Day",
                    }
                    if meter:
                        response = await self.client.get_pds_consumption(
                            self.local_id, meter.identifier, payload
                        )
                    else:
                        response = await self.client.get_dwelling_consumption(
                            self.local_id, fluid, payload
                        )
                    items = _parse_daily_response(response, expected_unit)
                else:
                    items = None
                if items is not None:
                    if any(not request_start <= item.day <= end for item in items):
                        raise OceaApiError("Daily consumption lies outside the requested period")
                    for item in items:
                        history.days[item.day] = item.value
                    if advance_cursor:
                        history.cursor = end + timedelta(days=1)
                    elif rechecking:
                        recheck_cursor = end + timedelta(days=1)
                        history.recheck_cursor = (
                            history.start
                            if recheck_cursor > today - timedelta(days=1)
                            else recheck_cursor
                        )
            except OceaApiError as err:
                _LOGGER.warning("Unable to retrieve one Ocea history period: %s", err)
            except (ClientError, TimeoutError):
                _LOGGER.warning("Unable to retrieve one Ocea history period")

        if key not in self._histories or history != previous:
            updated = {**self._histories, key: history}
            await self.store.async_save(_serialize_histories(self._generation, updated))
            self._histories = updated
        changed_days = [
            day for day, value in history.days.items() if previous.days.get(day) != value
        ]
        if not changed_days and key in self._published:
            return
        changed_since = min(changed_days) if key in self._published else None
        statistics = _daily_statistics(history.start, history.days, changed_since)
        if not statistics:
            return
        async_add_external_statistics(self.hass, metadata, statistics)
        self._published.add(key)
