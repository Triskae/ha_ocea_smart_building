"""Synchronize dated OCEA consumption as external statistics."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    PDS_FLUID_METADATA,
)
from .coordinator import (
    OCEADailyConsumption,
    OCEADataUpdateCoordinator,
    OCEAMeterReading,
    daily_consumptions_from_series,
)
from .ocea import OCEAApiClient, OCEAApiError, OCEAAuthError, OCEAMeter, OCEAResident
from .ocea.const import OCEA_TIME_ZONE
from .ocea.models import OCEAConsumptionRequest, OCEAConsumptionSeries

_LOGGER = logging.getLogger(__name__)
_CHUNK_DAYS: Final = 90
_MAX_CONCURRENT_REQUESTS: Final = 3
_MAX_RETRIES: Final = 5
_RETRY_BASE_SECONDS: Final = 60
_RETRY_MAX_SECONDS: Final = 21_600
_RECENT_OVERLAP_DAYS: Final = 3
_OLD_HISTORY_RECHECK_DAYS: Final = 7
_DAILY_SYNC_INTERVAL: Final = timedelta(days=1)
_RESTORE_STATISTICS_LIMIT: Final = 100_000


def history_store(hass: HomeAssistant, storage_key: str) -> Store[dict[str, object]]:
    """Create the private store of historical readings for a storage key."""
    return Store(hass, 1, f"{DOMAIN}_history.{storage_key}", private=True, atomic_writes=True)


@dataclass(slots=True)
class MeterHistory:
    """Persisted consumption and next unprocessed date for a single meter."""

    start: date
    cursor: date
    days: dict[date, Decimal] = field(default_factory=dict)
    next_request: datetime | None = None
    recheck_cursor: date | None = None
    retry_count: int = 0
    chunk_days: int = _CHUNK_DAYS
    last_old_recheck: date | None = None
    request_floor: date | None = None


def _api_date(value: object) -> date:
    """Parse an OCEA calendar date without exposing the source value."""
    if not isinstance(value, str):
        raise OCEAApiError("Missing or invalid OCEA date")
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value).date()
    except ValueError as err:
        raise OCEAApiError("Missing or invalid OCEA date") from err


def _occupation_start(
    resident: OCEAResident, local_id: str, today: date
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
        raise OCEAApiError("No unique active OCEA occupation for the dwelling")
    return matching[0]


def _history_start(occupation_start: date, meter: OCEAMeter) -> date:
    """Bound a meter's history by both occupation and device installation."""
    return max(occupation_start, _api_date(meter.installation_date))


def _dwelling_history_start(
    occupation_start: date, fluid: str, readings: Iterable[OCEAMeterReading]
) -> date:
    """Bound dwelling history by the earliest known installation for the fluid."""
    dates: list[date] = []
    matched = False
    invalid = False
    for reading in readings:
        if reading.meter.pds.fluid != fluid:
            continue
        matched = True
        try:
            dates.append(_api_date(reading.meter.installation_date))
        except OCEAApiError:
            invalid = True
    if not matched:
        _LOGGER.warning("No meter found for one OCEA dwelling fluid; using occupation start")
    elif invalid:
        if dates:
            _LOGGER.warning(
                "Missing or invalid meter installation date for one OCEA dwelling fluid; "
                "using the earliest valid date"
            )
        else:
            _LOGGER.warning(
                "No valid meter installation date for one OCEA dwelling fluid; "
                "using occupation start"
            )
    if not dates:
        return occupation_start
    return max(occupation_start, min(dates))


def _history_id(local_id: str, meter: OCEAMeter) -> str:
    """Build a stable fluid-independent key from the dwelling and measurement point."""
    return sha256(f"{local_id}\0{meter.identifier}".encode()).hexdigest()


def _dwelling_history_id(local_id: str) -> str:
    """Build a stable fluid-independent key for one dwelling."""
    return sha256(local_id.encode()).hexdigest()


def _statistic_id(key: str, fluid: str) -> str:
    """Return a stable Recorder ID without exposing OCEA identifiers."""
    return f"{DOMAIN}:{key}_{PDS_FLUID_METADATA[fluid].fluid_key}_consumption"


def _meter_statistic_id(local_id: str, meter: OCEAMeter) -> str:
    """Return the Recorder ID of a PDS consumption series."""
    return _statistic_id(_history_id(local_id, meter), meter.pds.fluid)


def _dwelling_statistic_id(local_id: str, fluid: str) -> str:
    """Return the Recorder ID of a dwelling consumption series."""
    return _statistic_id(_dwelling_history_id(local_id), fluid)


def _meter_metadata(local_id: str, meter: OCEAMeter) -> StatisticMetaData:
    """Build Recorder metadata for an existing PDS series."""
    fluid_metadata = PDS_FLUID_METADATA[meter.pds.fluid]
    serial_number = meter.serial_number or _history_id(local_id, meter)[:8]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=(
            f"OCEA - {local_id} - {serial_number} - "
            f"{fluid_metadata.statistic_display_label} consumption"
        ),
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
        name=f"OCEA - {local_id} - {fluid_metadata.statistic_display_label} consumption",
        source=DOMAIN,
        statistic_id=_dwelling_statistic_id(local_id, fluid),
        unit_class=fluid_metadata.statistic_unit_class,
        unit_of_measurement=fluid_metadata.statistic_unit_of_measurement,
    )


def _decimal_value(value: object) -> Decimal:
    """Validate a non-negative, finite consumption value."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise OCEAApiError("Invalid daily consumption value")
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as err:
        raise OCEAApiError("Invalid daily consumption value") from err
    if not result.is_finite() or result < 0:
        raise OCEAApiError("Invalid daily consumption value")
    return result


def _parse_daily_response(
    response: OCEAConsumptionSeries, expected_unit: str
) -> tuple[OCEADailyConsumption, ...]:
    """Validate a daily response without assuming missing values are zero."""
    if response.unit != expected_unit:
        raise OCEAApiError("Unsupported daily consumption unit")
    items = daily_consumptions_from_series(response)
    if items is None:
        raise OCEAApiError("Invalid daily consumption list")
    readings: list[OCEADailyConsumption] = []
    seen: set[date] = set()
    for item in items:
        if item.consent is False:
            raise OCEAApiError("OCEA does not provide consent for daily consumption")
        if item.day in seen:
            raise OCEAApiError("Duplicate daily consumption date")
        if not item.value.is_finite() or item.value < 0:
            raise OCEAApiError("Invalid daily consumption value")
        seen.add(item.day)
        readings.append(item)
    return tuple(readings)


def _current_daily_readings(
    reading: OCEAMeterReading, expected_unit: str, today: date
) -> tuple[OCEADailyConsumption, ...]:
    """Validate coordinator readings before reusing them for history."""
    if reading.unit != expected_unit or reading.daily_consumptions is None:
        raise OCEAApiError("Invalid current daily consumption unit or readings")
    seen: set[date] = set()
    for item in reading.daily_consumptions:
        if (
            item.consent is False
            or item.day in seen
            or not today.replace(day=1) <= item.day <= today
            or not item.value.is_finite()
            or item.value < 0
        ):
            raise OCEAApiError("Invalid current daily consumption")
        seen.add(item.day)
    return reading.daily_consumptions


def _load_histories(data: dict[str, object] | None) -> tuple[str, dict[str, MeterHistory]]:
    """Validate persisted history before using it."""
    if data is None:
        return uuid4().hex, {}
    generation = data.get("generation")
    meters = data.get("meters")
    if not isinstance(generation, str) or not generation or not isinstance(meters, dict):
        raise OCEAApiError("Invalid persisted consumption history")
    histories: dict[str, MeterHistory] = {}
    for key, raw in meters.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise OCEAApiError("Invalid persisted consumption history")
        start = _api_date(raw.get("start"))
        cursor = _api_date(raw.get("cursor"))
        raw_next_request = raw.get("next_request")
        next_request: datetime | None = None
        retry_count = raw.get("retry_count", 0)
        chunk_days = raw.get("chunk_days", _CHUNK_DAYS)
        last_old_recheck = (
            _api_date(raw["last_old_recheck"])
            if raw.get("last_old_recheck") is not None
            else None
        )
        request_floor = (
            _api_date(raw["request_floor"]) if raw.get("request_floor") is not None else None
        )
        if not isinstance(retry_count, int) or isinstance(retry_count, bool):
            raise OCEAApiError("Invalid persisted consumption history")
        if not isinstance(chunk_days, int) or isinstance(chunk_days, bool) or not 1 <= chunk_days <= _CHUNK_DAYS:
            raise OCEAApiError("Invalid persisted consumption history")
        if raw_next_request is not None and "retry_count" in raw:
            if not isinstance(raw_next_request, str):
                raise OCEAApiError("Invalid persisted consumption history")
            try:
                next_request = datetime.fromisoformat(raw_next_request)
            except ValueError as err:
                raise OCEAApiError("Invalid persisted consumption history") from err
            if next_request.utcoffset() is None:
                raise OCEAApiError("Invalid persisted consumption history")
        recheck_cursor = _api_date(raw.get("recheck_cursor", raw.get("start")))
        raw_days = raw.get("days")
        if (
            cursor < start
            or recheck_cursor < start
            or (request_floor is not None and request_floor < start)
            or not isinstance(raw_days, dict)
        ):
            raise OCEAApiError("Invalid persisted consumption history")
        days: dict[date, Decimal] = {}
        for day, value in raw_days.items():
            parsed_day = _api_date(day)
            if parsed_day < start:
                raise OCEAApiError("Invalid persisted consumption history")
            days[parsed_day] = _decimal_value(value)
        histories[key] = MeterHistory(
            start,
            cursor,
            days,
            next_request,
            recheck_cursor,
            retry_count,
            chunk_days,
            last_old_recheck,
            request_floor,
        )
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
                "retry_count": history.retry_count,
                "chunk_days": history.chunk_days,
                "last_old_recheck": (
                    history.last_old_recheck.isoformat()
                    if history.last_old_recheck
                    else None
                ),
                "request_floor": (
                    history.request_floor.isoformat() if history.request_floor else None
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


class OCEAHistoryManager:
    """Backfill and update external statistics for meters and dwelling fluids."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OCEAApiClient,
        coordinator: OCEADataUpdateCoordinator,
        local_id: str,
        storage_key: str,
    ) -> None:
        self.hass = hass
        self.client = client
        self.coordinator = coordinator
        self.local_id = local_id
        self.store = history_store(hass, storage_key)
        self._task: asyncio.Task[None] | None = None
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._histories: dict[str, MeterHistory] | None = None
        self._generation: str | None = None
        self._published: set[str] = set()
        self._active_series: set[str] = set()
        self._occupation_start: date | None = None
        self._dwelling_fluids: tuple[str, ...] | None = None
        self._discovery_checked_at: datetime | None = None
        self._global_retry_count = 0
        self._global_next_request: datetime | None = None
        self._request_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._store_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()

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
                except OCEAApiError:
                    _LOGGER.warning("Ignoring one invalid restored OCEA statistic value")
                    continue
                history_start = min(history_start, local_start.date())

        if not days:
            return None
        _LOGGER.debug("Restored %d OCEA daily readings from Recorder", len(days))
        return MeterHistory(
            history_start,
            start,
            days,
            recheck_cursor=history_start,
        )

    @callback
    def async_start(self) -> None:
        """Start the dwelling-scoped backfill and daily synchronization worker."""
        if self._stopped:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = self.hass.async_create_background_task(
            self._async_run(), "OCEA consumption history"
        )

    async def async_stop(self) -> None:
        """Stop the history worker and wait for its cleanup."""
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            if (task := self._task) is None:
                return
            if not task.done():
                task.cancel("OCEA history manager stopping")
            try:
                await task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise

    async def _async_run(self) -> None:
        """Run backfill continuously, then synchronize once per day."""
        while True:
            failed = False
            try:
                await self._async_sync()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "Unexpected error synchronizing OCEA consumption history (%s)",
                    type(err).__name__,
                )
                failed = True

            try:
                now = dt_util.utcnow()
                if self._global_next_request is not None and self._global_next_request > now:
                    delay = (self._global_next_request - now).total_seconds()
                elif (retry_delay := self._backfill_delay()) is not None:
                    delay = retry_delay
                elif self._has_pending_backfill():
                    delay = 0
                else:
                    delay = _DAILY_SYNC_INTERVAL.total_seconds()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "Unable to schedule OCEA consumption history synchronization (%s)",
                    type(err).__name__,
                )
                delay = _RETRY_BASE_SECONDS

            if failed:
                delay = max(delay, _RETRY_BASE_SECONDS)
            await asyncio.sleep(delay)

    def _has_pending_backfill(self) -> bool:
        """Return whether a known series has unprocessed consolidated days."""
        if self._histories is None:
            return False
        last_complete_day = dt_util.now().astimezone(OCEA_TIME_ZONE).date() - timedelta(days=1)
        return any(
            self._histories[key].cursor <= last_complete_day
            for key in self._active_series
            if key in self._histories
        )

    def _backfill_delay(self) -> float | None:
        """Return the earliest persisted retry delay, if any."""
        if self._histories is None:
            return None
        now = dt_util.utcnow()
        last_complete_day = now.astimezone(OCEA_TIME_ZONE).date() - timedelta(days=1)
        if any(
            (history := self._histories.get(key)) is not None
            and history.cursor <= last_complete_day
            and (history.next_request is None or history.next_request <= now)
            for key in self._active_series
        ):
            return 0
        retries = [
            (history.next_request - now).total_seconds()
            for key in self._active_series
            if (history := self._histories.get(key)) is not None
            if history.next_request is not None
            and history.next_request > now
        ]
        return max(1.0, min(retries)) if retries else None

    async def _async_sync(self) -> None:
        """Synchronize each meter and dwelling fluid independently."""
        async with self._sync_lock:
            await self._async_sync_locked()

    async def _async_sync_locked(self) -> None:
        """Perform one pass over all discovered statistics series."""
        self._active_series = set()
        data = self.coordinator.data
        if data is None:
            return
        now = dt_util.utcnow()
        try:
            if self._histories is None:
                try:
                    self._generation, self._histories = _load_histories(
                        await self.store.async_load()
                    )
                except OCEAApiError as err:
                    _LOGGER.warning(
                        "Invalid cached OCEA history; rebuilding from the API: %s", err
                    )
                    self._generation = uuid4().hex
                    self._histories = {}
                    await self.store.async_save(
                        _serialize_histories(self._generation, self._histories)
                    )
            local_now = dt_util.now().astimezone(OCEA_TIME_ZONE)
            now = dt_util.as_utc(local_now)
            if self._global_next_request is not None and now < self._global_next_request:
                return
            refresh_discovery = (
                self._discovery_checked_at is None
                or now - self._discovery_checked_at >= _DAILY_SYNC_INTERVAL
            )
            if refresh_discovery:
                calendar_today = local_now.date()
                occupation_start = _occupation_start(
                    await self.client.get_resident(), self.local_id, calendar_today
                )
                dwelling_fluids = tuple(
                    await self.client.get_dwelling_fluids(self.local_id)
                )
                self._occupation_start = occupation_start
                self._dwelling_fluids = dwelling_fluids
                self._discovery_checked_at = now
            if self._occupation_start is None or self._dwelling_fluids is None:
                raise OCEAApiError("OCEA history discovery is unavailable")
            occupation_start = self._occupation_start
            dwelling_fluids = self._dwelling_fluids
            today = local_now.date() - timedelta(days=1)
            self._global_retry_count = 0
            self._global_next_request = None
        except OCEAApiError as err:
            self._set_global_retry(now)
            _LOGGER.warning("Unable to synchronize OCEA consumption history: %s", err)
            return
        except (OCEAAuthError, ClientError, TimeoutError, OSError):
            self._set_global_retry(now)
            _LOGGER.warning("Unable to synchronize OCEA consumption history")
            return

        unsupported_count = len(set(dwelling_fluids) - PDS_FLUID_METADATA.keys())
        if unsupported_count:
            _LOGGER.debug("Ignoring %d unsupported dwelling fluids", unsupported_count)
        series = [
            (
                "meter",
                reading,
                lambda reading=reading: self._async_sync_meter(
                    reading, occupation_start, today, now
                ),
            )
            for reading in data.meters.values()
        ] + [
            (
                "dwelling",
                fluid,
                lambda fluid=fluid: self._async_sync_series(
                    fluid,
                    _dwelling_history_start(occupation_start, fluid, data.meters.values()),
                    today,
                    now,
                    None,
                ),
            )
            for fluid in sorted(set(dwelling_fluids) & PDS_FLUID_METADATA.keys())
        ]
        self._active_series = set()

        async def sync_one(
            kind: str, series_item: object, sync: Callable[[], Awaitable[None]]
        ) -> tuple[str, object, Exception | None]:
            try:
                async with self._request_semaphore:
                    await sync()
            except Exception as err:  # noqa: BLE001
                # Wait for the other series before propagating unexpected errors.
                return kind, series_item, err
            return kind, series_item, None

        results = await asyncio.gather(
            *(sync_one(kind, item, sync) for kind, item, sync in series)
        )
        authentication_failed = False
        for kind, _, error in results:
            if error is None:
                continue
            if isinstance(error, OCEAAuthError):
                authentication_failed = True
                _LOGGER.warning("Unable to authenticate while synchronizing OCEA history")
            elif isinstance(error, OCEAApiError):
                _LOGGER.warning("Unable to synchronize one OCEA %s history: %s", kind, error)
            elif isinstance(error, (ClientError, TimeoutError, OSError)):
                _LOGGER.warning("Unable to synchronize one OCEA %s history", kind)
            else:
                raise error
        if authentication_failed:
            self._set_global_retry(now)
            return

    async def _async_sync_meter(
        self, reading: OCEAMeterReading, occupation_start: date, today: date, now: datetime
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
        reading: OCEAMeterReading | None,
    ) -> None:
        """Update one series and publish recalculated cumulative sums."""
        assert self._histories is not None
        assert self._generation is not None
        fluid_metadata = PDS_FLUID_METADATA.get(fluid)
        if fluid_metadata is None:
            raise OCEAApiError("Unsupported consumption fluid")
        expected_unit = fluid_metadata.api_unit
        meter = reading.meter if reading is not None else None
        start = _history_start(occupation_start, meter) if meter else occupation_start
        if start > today + timedelta(days=1):
            raise OCEAApiError("OCEA consumption history begins in the future")
        metadata = (
            _meter_metadata(self.local_id, meter)
            if meter else _dwelling_metadata(self.local_id, fluid)
        )
        key = metadata["statistic_id"]
        self._active_series.add(key)
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
        previous_floor = previous.request_floor or previous.start
        if start < previous_floor:
            cursor = min(cursor, start)
            recheck_cursor = min(recheck_cursor, start)
        cursor = max(cursor, start)
        recheck_cursor = max(recheck_cursor, start)
        history = MeterHistory(
            history_start,
            cursor,
            previous.days.copy(),
            previous.next_request,
            recheck_cursor,
            previous.retry_count,
            previous.chunk_days,
            previous.last_old_recheck,
            start,
        )
        if history.cursor > today and history.last_old_recheck is None:
            history.last_old_recheck = today
        current_items = None
        if reading is not None:
            try:
                current_items = _current_daily_readings(
                    reading, expected_unit, today + timedelta(days=1)
                )
            except OCEAApiError:
                _LOGGER.warning("Unable to use current daily readings for one OCEA meter")

        older_recheck = False
        if history.cursor <= today:
            request_start = history.cursor
            end = min(request_start + timedelta(days=history.chunk_days - 1), today)
            advance_cursor = True
        elif (request_floor := history.request_floor or history.start) < today:
            old_history_end = today - timedelta(days=_RECENT_OVERLAP_DAYS)
            if (
                history.last_old_recheck is None
                or today - history.last_old_recheck >= timedelta(days=_OLD_HISTORY_RECHECK_DAYS)
            ) and request_floor <= old_history_end:
                request_start = history.recheck_cursor or request_floor
                if request_start > old_history_end:
                    request_start = request_floor
                end = min(
                    request_start + timedelta(days=history.chunk_days - 1),
                    old_history_end,
                )
                older_recheck = True
            else:
                request_start = max(
                    request_floor,
                    today - timedelta(days=_RECENT_OVERLAP_DAYS - 1),
                )
                end = today
            advance_cursor = False
        else:
            request_start = None
            end = None
            advance_cursor = False

        if current_items is not None and history.cursor <= today:
            for item in current_items:
                if start <= item.day <= today:
                    history.days[item.day] = item.value

        if (
            request_start is not None
            and end is not None
            and (history.next_request is None or now >= history.next_request)
        ):
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
                else:
                    while True:
                        payload: OCEAConsumptionRequest = {
                            "debut": f"{request_start.isoformat()}T00:00:00.000Z",
                            "fin": f"{end.isoformat()}T23:59:59.999Z",
                            "granularity": "Day",
                        }
                        try:
                            if meter:
                                response = await self.client.get_pds_consumption(
                                    self.local_id, meter.identifier, payload
                                )
                            else:
                                response = await self.client.get_dwelling_consumption(
                                    self.local_id, fluid, payload
                                )
                            items = _parse_daily_response(response, expected_unit)
                        except OCEAApiError as err:
                            if err.status != 413 or request_start >= end:
                                raise
                            range_days = (end - request_start).days + 1
                            history.chunk_days = max(
                                1,
                                min(history.chunk_days, range_days // 2),
                            )
                            end = min(
                                request_start
                                + timedelta(days=history.chunk_days - 1),
                                end,
                            )
                            continue
                        break
                if any(not request_start <= item.day <= end for item in items):
                    raise OCEAApiError(
                        "Daily consumption lies outside the requested period"
                    )
                for item in items:
                    history.days[item.day] = item.value
                if advance_cursor:
                    history.cursor = end + timedelta(days=1)
                elif older_recheck:
                    next_recheck = end + timedelta(days=1)
                    history.recheck_cursor = (
                        history.request_floor or history.start
                        if next_recheck
                        > today - timedelta(days=_RECENT_OVERLAP_DAYS)
                        else next_recheck
                    )
                    history.last_old_recheck = today
                history.retry_count = 0
                history.next_request = None
            except OCEAApiError as err:
                self._set_retry(history, now)
                _LOGGER.warning("Unable to retrieve one OCEA history period: %s", err)
            except (ClientError, TimeoutError, OSError):
                self._set_retry(history, now)
                _LOGGER.warning("Unable to retrieve one OCEA history period")

        if key not in self._histories or history != previous:
            async with self._store_lock:
                updated = {**self._histories, key: history}
                await self.store.async_save(
                    _serialize_histories(self._generation, updated)
                )
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

    @staticmethod
    def _set_retry(history: MeterHistory, now: datetime) -> None:
        """Persist a bounded exponential delay for the failed range."""
        history.retry_count = min(history.retry_count + 1, _MAX_RETRIES)
        delay = (
            _DAILY_SYNC_INTERVAL.total_seconds()
            if history.retry_count >= _MAX_RETRIES
            else min(
                _RETRY_BASE_SECONDS * 2 ** (history.retry_count - 1),
                _RETRY_MAX_SECONDS,
            )
        )
        history.next_request = now + timedelta(seconds=delay)

    def _set_global_retry(self, now: datetime) -> None:
        """Back off failed resident or fluid discovery calls."""
        self._global_retry_count = min(self._global_retry_count + 1, _MAX_RETRIES)
        delay = (
            _DAILY_SYNC_INTERVAL.total_seconds()
            if self._global_retry_count >= _MAX_RETRIES
            else min(
                _RETRY_BASE_SECONDS * 2 ** (self._global_retry_count - 1),
                _RETRY_MAX_SECONDS,
            )
        )
        self._global_next_request = now + timedelta(seconds=delay)
