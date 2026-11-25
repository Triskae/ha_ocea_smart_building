"""Tests for dated OCEA external statistics."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from hashlib import sha256
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.ocea_smart_building.const import (
    CETC_FLUID,
    CONF_LOCAL_ID,
    DOMAIN,
    PDS_FLUID_METADATA,
)
from custom_components.ocea_smart_building.coordinator import (
    OCEADailyConsumption,
    OCEAData,
    OCEADataUpdateCoordinator,
    OCEAMeterReading,
)
from custom_components.ocea_smart_building.coordinator import (
    _build_meter_data as build_meter_data,
)
from custom_components.ocea_smart_building.history import (
    OCEAHistoryManager,
    _current_daily_readings,
    _daily_statistics,
    _dwelling_history_id,
    _dwelling_metadata,
    _dwelling_statistic_id,
    _history_id,
    _history_start,
    _meter_metadata,
    _meter_statistic_id,
    _occupation_start,
    _parse_daily_response,
    history_store,
)
from custom_components.ocea_smart_building.ocea import (
    OCEAApiClient,
    OCEAApiError,
    OCEAMeter,
    OCEAResident,
)
from custom_components.ocea_smart_building.ocea.models import (
    OCEAConsumptionResponse,
    OCEAConsumptionSeries,
    OCEADeviceData,
    OCEAPdsData,
)


@pytest.fixture(autouse=True)
def mock_recorder_restore_without_real_recorder(request: pytest.FixtureRequest):
    """Keep unit synchronizer tests independent of the Recorder database."""
    if "real_recorder" in request.fixturenames:
        yield
        return
    with patch(
        "custom_components.ocea_smart_building.history.get_last_statistics",
        return_value={},
    ):
        yield


def _build_meter_data(
    meter: OCEAMeter, response: OCEAConsumptionResponse
) -> OCEAMeterReading:
    """Build readings from a synthetic API response at the client boundary."""
    return build_meter_data(meter, OCEAConsumptionSeries.from_api(response))


def _meter(
    installation_date: str | None = "2024-01-03T00:00:00Z",
    fluid: str = "EauFroide",
    pds_id: str = "pds-1",
) -> OCEAMeter:
    return OCEAMeter.from_api(
        {"id": pds_id, "fluide": fluid},
        {"datePose": installation_date, "numeroSerie": "example"},
    )


def _history_manager(hass: HomeAssistant) -> OCEAHistoryManager:
    """Create a manager for worker lifecycle tests."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    client = AsyncMock(spec=OCEAApiClient)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    return OCEAHistoryManager(
        hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
    )


async def test_history_manager_start_is_idempotent_and_stop_waits_for_worker(
    hass: HomeAssistant,
) -> None:
    """A repeated start creates one worker, and stop waits for its cancellation."""
    manager = _history_manager(hass)
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    with (
        patch.object(manager, "_async_run", side_effect=run),
        patch.object(
            manager.hass,
            "async_create_background_task",
            wraps=manager.hass.async_create_background_task,
        ) as create_background_task,
    ):
        manager.async_start()
        task = manager._task
        assert task is not None
        manager.async_start()
        assert manager._task is task
        create_background_task.assert_called_once()
        await asyncio.wait_for(started.wait(), timeout=1)

        await manager.async_stop()
        await manager.async_stop()

    assert cleaned_up.is_set()
    assert task.done()
    assert task.cancelled()


async def test_history_manager_stop_without_worker_is_terminal(
    hass: HomeAssistant,
) -> None:
    """Stopping before start is safe and prevents a later worker from starting."""
    manager = _history_manager(hass)

    await manager.async_stop()
    await manager.async_stop()
    manager.async_start()

    assert manager._task is None


async def test_history_manager_stop_after_worker_finished(hass: HomeAssistant) -> None:
    """Stopping a normally finished worker does not fail."""
    manager = _history_manager(hass)

    with patch.object(manager, "_async_run", new_callable=AsyncMock):
        manager.async_start()
        task = manager._task
        assert task is not None
        await task
        await manager.async_stop()

    assert task.done()
    assert not task.cancelled()


async def test_history_manager_stop_surfaces_worker_error(hass: HomeAssistant) -> None:
    """Unexpected worker errors remain visible when the worker is stopped."""
    manager = _history_manager(hass)
    finished = asyncio.Event()

    async def run() -> None:
        try:
            await asyncio.sleep(0)
            raise RuntimeError("history worker failed")
        finally:
            finished.set()

    with patch.object(manager, "_async_run", side_effect=run):
        manager.async_start()
        task = manager._task
        assert task is not None
        await asyncio.wait_for(finished.wait(), timeout=1)

        with pytest.raises(RuntimeError, match="history worker failed"):
            await manager.async_stop()
        await manager.async_stop()

    assert task.done()


@pytest.mark.parametrize(
    ("global_delay", "backfill_delay", "expected_delay"),
    [(None, 0, 60), (None, 180, 180), (300, 0, 300)],
)
async def test_history_worker_recovers_after_unexpected_error(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    global_delay: int | None,
    backfill_delay: int,
    expected_delay: int,
) -> None:
    """Wait for the appropriate retry delay before resuming after an error."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    client = AsyncMock(spec=OCEAApiClient)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    synchronizer = OCEAHistoryManager(
        hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
    )
    now = datetime(2024, 2, 1, tzinfo=UTC)
    if global_delay is not None:
        synchronizer._global_next_request = now + timedelta(seconds=global_delay)
    waiting = asyncio.Event()
    release = asyncio.Event()
    resumed = asyncio.Event()

    async def sync() -> None:
        if not waiting.is_set():
            raise RuntimeError("sensitive-placeholder")
        resumed.set()

    async def sleep(delay: float) -> None:
        if not waiting.is_set():
            waiting.set()
            await release.wait()
            return
        await asyncio.Event().wait()

    with (
        patch.object(synchronizer, "_async_sync", side_effect=sync) as sync_mock,
        patch.object(synchronizer, "_backfill_delay", return_value=backfill_delay),
        patch("custom_components.ocea_smart_building.history.dt_util.utcnow", return_value=now),
        patch("custom_components.ocea_smart_building.history.asyncio.sleep", side_effect=sleep)
        as sleep_mock,
    ):
        task = asyncio.create_task(synchronizer._async_run())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert sync_mock.await_count == 1
            sleep_mock.assert_awaited_once_with(expected_delay)
            assert "Unexpected error synchronizing OCEA consumption history" in caplog.text
            assert "RuntimeError" in caplog.text
            assert "sensitive-placeholder" not in caplog.text
            release.set()
            await asyncio.wait_for(resumed.wait(), timeout=1)
            assert sync_mock.await_count == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_history_worker_cancellation_during_sync(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Cancellation during synchronization propagates without an error log."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    client = AsyncMock(spec=OCEAApiClient)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    synchronizer = OCEAHistoryManager(
        hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
    )
    started = asyncio.Event()

    async def sync() -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(synchronizer, "_async_sync", side_effect=sync):
        task = asyncio.create_task(synchronizer._async_run())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert "Unexpected error synchronizing OCEA consumption history" not in caplog.text


async def test_history_worker_cancels_other_series_after_unexpected_error(
    hass: HomeAssistant,
) -> None:
    """Keep concurrent series attached to the worker until they finish or are cancelled."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = []
    meters = [_meter(pds_id=f"pds-{index}") for index in range(2)]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(
        totals={},
        meters={
            meter.identifier: OCEAMeterReading(meter=meter, value=None, latest_date=None)
            for meter in meters
        },
    ))
    synchronizer = OCEAHistoryManager(
        hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
    )
    second_started = asyncio.Event()
    first_failed = asyncio.Event()
    second_finished = asyncio.Event()
    release = asyncio.Event()

    async def sync_meter(
        reading: OCEAMeterReading, occupation_start: date, today: date, now: datetime
    ) -> None:
        if reading.meter.identifier == meters[0].identifier:
            await second_started.wait()
            first_failed.set()
            raise RuntimeError("sensitive-placeholder")
        second_started.set()
        try:
            await release.wait()
        finally:
            second_finished.set()

    with patch.object(synchronizer, "_async_sync_meter", side_effect=sync_meter):
        task = asyncio.create_task(synchronizer._async_run())
        try:
            await asyncio.wait_for(first_failed.wait(), timeout=1)
            for _ in range(3):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert second_finished.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_history_worker_recovers_after_scheduling_error(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected scheduling error also waits before the next iteration."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    client = AsyncMock(spec=OCEAApiClient)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    synchronizer = OCEAHistoryManager(
        hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
    )
    waiting = asyncio.Event()
    release = asyncio.Event()
    resumed = asyncio.Event()

    async def sync() -> None:
        if waiting.is_set():
            resumed.set()

    async def sleep(delay: float) -> None:
        if not waiting.is_set():
            waiting.set()
            await release.wait()
            return
        await asyncio.Event().wait()

    with (
        patch.object(synchronizer, "_async_sync", side_effect=sync) as sync_mock,
        patch.object(
            synchronizer, "_backfill_delay", side_effect=[RuntimeError("sensitive-placeholder"), 0]
        ),
        patch("custom_components.ocea_smart_building.history.asyncio.sleep", side_effect=sleep)
        as sleep_mock,
    ):
        task = asyncio.create_task(synchronizer._async_run())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert sync_mock.await_count == 1
            sleep_mock.assert_awaited_once_with(60)
            assert "Unable to schedule OCEA consumption history synchronization" in caplog.text
            assert "RuntimeError" in caplog.text
            assert "sensitive-placeholder" not in caplog.text
            release.set()
            await asyncio.wait_for(resumed.wait(), timeout=1)
            assert sync_mock.await_count == 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def test_occupation_and_meter_dates_are_distinct() -> None:
    resident = OCEAResident.from_api({
        "occupations": [
            {"logementId": "other", "dateDebut": "2022-01-01T00:00:00Z", "dateFin": None},
            {"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None},
        ]
    })
    start = _occupation_start(resident, "local", date(2024, 3, 1))
    assert start == date(2024, 1, 1)
    assert _history_start(start, _meter()) == date(2024, 1, 3)
    assert _history_start(date(2024, 1, 4), _meter()) == date(2024, 1, 4)
    with pytest.raises(OCEAApiError):
        _history_start(start, _meter(None))


@pytest.mark.parametrize(
    ("start", "end", "today", "expected_start"),
    [
        ("2024-01-01T00:00:00+01:00", None, date(2024, 1, 1), date(2024, 1, 1)),
        (
            "2024-03-31T00:00:00+01:00",
            "2024-10-27T00:00:00+02:00",
            date(2024, 10, 27),
            date(2024, 3, 31),
        ),
        ("2024-12-31T23:30:00-03:00", None, date(2024, 12, 31), date(2024, 12, 31)),
    ],
)
def test_occupation_dates_use_api_calendar_days(
    start: str, end: str | None, today: date, expected_start: date
) -> None:
    """Keep the supplied calendar days despite crossing midnight in UTC."""
    resident = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": start, "dateFin": end}]
    })
    assert _occupation_start(resident, "local", today) == expected_start


@pytest.mark.parametrize(
    "occupations",
    [
        [{"logementId": "local", "dateFin": None}],
        [
            {"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None},
            {"logementId": "local", "dateDebut": "2024-01-02T00:00:00Z", "dateFin": None},
        ],
    ],
)
async def test_history_does_not_fetch_without_unique_occupation(
    hass: HomeAssistant, occupations: list[dict[str, object]], caplog: pytest.LogCaptureFixture
) -> None:
    """Missing or ambiguous occupation dates prevent historical requests."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({"occupations": occupations})
    meter = _meter()
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(
        totals={},
        meters={meter.identifier: OCEAMeterReading(meter=meter, value=None, latest_date=None)},
    ))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        return_value=datetime(2024, 2, 1, tzinfo=UTC),
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
    client.get_dwelling_fluids.assert_not_awaited()
    client.get_dwelling_consumption.assert_not_awaited()
    client.get_pds_consumption.assert_not_awaited()
    assert "No unique active OCEA occupation" in caplog.text


@pytest.mark.parametrize(
    ("occupation", "installations", "expected", "warning"),
    [
        ("2024-01-01", ["2024-01-12", "2024-01-05"], "2024-01-05", None),
        ("2024-01-10", ["2024-01-05", "2024-01-12"], "2024-01-10", None),
        ("2024-01-05", ["2024-01-05"], "2024-01-05", None),
        ("2024-01-01", [None, "2024-01-12", "private-installation"], "2024-01-12",
         "using the earliest valid date"),
        ("2024-01-01", [None, "private-installation"], "2024-01-01", "using occupation start"),
        ("2024-01-01", [], "2024-01-01", "No meter found"),
    ],
)
async def test_dwelling_history_start_uses_selected_meters(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    occupation: str,
    installations: list[str | None],
    expected: str,
    warning: str | None,
) -> None:
    """Request from the earliest usable installation, bounded by occupation."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": f"{occupation}T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    empty = OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []})
    client.get_dwelling_consumption.return_value = empty
    client.get_pds_consumption.return_value = empty
    meters = [
        _meter(installation, pds_id=f"pds-{index}")
        for index, installation in enumerate(installations)
    ]
    meters.append(_meter("2023-01-01", fluid="EauChaude", pds_id="other"))
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(
        totals={},
        meters={
            meter.identifier: _build_meter_data(meter, {"unite": "m3", "consommations": []})
            for meter in meters
        },
    ))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        return_value=datetime(2024, 2, 1, tzinfo=UTC),
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
    assert client.get_dwelling_consumption.call_args.args[2] == {
        "debut": f"{expected}T00:00:00.000Z",
        "fin": "2024-01-31T23:59:59.999Z",
        "granularity": "Day",
    }
    if warning:
        assert warning in caplog.text
        assert "private-installation" not in caplog.text


@pytest.mark.parametrize("hot_installation", ["2024-01-20", "2024-03-01"])
async def test_dwelling_fluids_have_independent_installation_bounds(
    hass: HomeAssistant, hot_installation: str,
) -> None:
    """Each fluid has its own bound, and a future fluid cannot block another."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide", "EauChaude"]
    empty = OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []})
    client.get_dwelling_consumption.return_value = empty
    client.get_pds_consumption.return_value = empty
    cold = _meter("2024-01-10", pds_id="cold")
    hot = _meter(hot_installation, fluid="EauChaude", pds_id="hot")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(
        totals={},
        meters={
            meter.identifier: _build_meter_data(meter, {"unite": "m3", "consommations": []})
            for meter in (cold, hot)
        },
    ))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        return_value=datetime(2024, 2, 1, tzinfo=UTC),
    ):
        synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await synchronizer._async_sync()
    payloads = {
        call.args[1]: call.args[2] for call in client.get_dwelling_consumption.call_args_list
    }
    assert payloads["EauFroide"] == {
        "debut": "2024-01-10T00:00:00.000Z",
        "fin": "2024-01-31T23:59:59.999Z",
        "granularity": "Day",
    }
    if hot_installation == "2024-01-20":
        assert payloads["EauChaude"]["debut"] == "2024-01-20T00:00:00.000Z"
        assert len(payloads) == 2
    else:
        assert set(payloads) == {"EauFroide"}
    assert synchronizer._histories is not None
    assert synchronizer._histories[_meter_statistic_id("local", cold)].cursor == date(2024, 2, 1)


def test_history_identity_uses_only_dwelling_and_pds_and_fluid_in_statistic_id() -> None:
    """Keep keys fluid-independent and statistic IDs distinct per fluid and level."""
    meter = _meter()
    key = sha256(b"local\0pds-1").hexdigest()
    assert _history_id("local", meter) == key
    assert _history_id("local", _meter(fluid="EauChaude")) == key
    assert _history_id("other", meter) != key
    assert _history_id("local", _meter(pds_id="pds-2")) != key
    assert _meter_statistic_id("local", meter) == f"{DOMAIN}:{key}_cold_water_consumption"
    assert _meter_statistic_id("local", _meter("2025-01-01", pds_id="pds-1")) == (
        _meter_statistic_id("local", meter)
    )
    dwelling_key = sha256(b"local").hexdigest()
    assert _dwelling_history_id("local") == dwelling_key
    assert _dwelling_history_id("other") != dwelling_key
    assert _dwelling_statistic_id("local", "EauChaude") == (
        f"{DOMAIN}:{dwelling_key}_hot_water_consumption"
    )
    ids = {
        factory(fluid)
        for fluid in ("EauFroide", "EauChaude", CETC_FLUID)
        for factory in (
            lambda f: _dwelling_statistic_id("local", f),
            lambda f: _meter_statistic_id("local", _meter(fluid=f)),
        )
    }
    assert len(ids) == 6


def test_statistic_names_put_serial_after_local_id() -> None:
    """Name dwelling and PDS statistics with readable fluid labels."""
    assert _dwelling_metadata("local", "EauFroide")["name"] == (
        "OCEA - local - Cold water consumption"
    )
    meter = _meter(fluid="EauChaude")
    assert _meter_metadata("local", meter)["name"] == (
        f"OCEA - local - {meter.serial_number} - Hot water consumption"
    )
    assert meter.serial_number not in _dwelling_metadata("local", "EauChaude")["name"]


@pytest.mark.usefixtures("real_recorder")
async def test_dwelling_history_backfill_correction_and_restore(
    hass: HomeAssistant,
) -> None:
    """Keep water and heating aggregates correct across restart and cache loss."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide", "EauChaude", CETC_FLUID, "Other"]
    calls: dict[str, int] = {}

    def series(local_id, fluid, payload):
        assert local_id == "local"
        count = calls.get(fluid, 0)
        calls[fluid] = count + 1
        values = [1, 2] if count == 0 else [1.5, 2] if count == 1 else [1.5]
        return OCEAConsumptionSeries.from_api({
            "unite": "kWh" if fluid == CETC_FLUID else "m3",
            "consommations": [
                {"date": f"2024-03-0{index + 1}T00:00:00", "valeur": value,
                 "consentement": True,
                 "type": "ConsommationResponse" if fluid == CETC_FLUID
                 else "ConsommationEauResponse"}
                for index, value in enumerate(values)
            ],
        })

    client.get_dwelling_consumption.side_effect = series
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 3, 3, hour, tzinfo=UTC) for hour in (0, 4, 5, 10)
        ],
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        assert client.get_dwelling_consumption.await_count == 6
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry.entry_id).async_remove()
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)

    assert calls == {"EauFroide": 4, "EauChaude": 4, CETC_FLUID: 4}
    for call in client.get_dwelling_consumption.call_args_list:
        assert call.args[2]["debut"].startswith("2024-03-01")
        assert call.args[2]["granularity"] == "Day"
    ids = {_dwelling_statistic_id("local", fluid) for fluid in calls}
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=ids)
    )
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2024, 3, 1, tzinfo=UTC),
        None, ids, "hour", None, {"sum", "state"},
    )
    assert set(metadata) == ids
    for fluid in calls:
        statistic_id = _dwelling_statistic_id("local", fluid)
        assert metadata[statistic_id][1]["unit_of_measurement"] == (
            UnitOfEnergy.KILO_WATT_HOUR if fluid == CETC_FLUID else UnitOfVolume.CUBIC_METERS
        )
        assert metadata[statistic_id][1]["unit_class"] == (
            "energy" if fluid == CETC_FLUID else "volume"
        )
        label = {
            "EauFroide": "Cold water",
            "EauChaude": "Hot water",
            CETC_FLUID: "Heating energy",
        }[fluid]
        assert metadata[statistic_id][1]["name"] == f"OCEA - local - {label} consumption"
        assert [row["sum"] for row in rows[statistic_id]] == pytest.approx([0, 1.5, 3.5])


@pytest.mark.usefixtures("real_recorder")
async def test_dwelling_changed_bound_limits_rechecks_and_preserves_recorded_values(
    hass: HomeAssistant,
) -> None:
    """Constrain requests after the bound changes without deleting old data."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    key = _dwelling_statistic_id("local", "EauFroide")
    await history_store(hass, entry.entry_id).async_save({
        "generation": "generation",
        "meters": {key: {
            "start": "2024-01-01",
            "cursor": "2024-01-02",
            "days": {"2024-01-01": "1"},
            "recheck_cursor": "2024-01-01",
            "next_request": None,
        }},
    })
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    empty = OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []})
    client.get_pds_consumption.return_value = empty
    client.get_dwelling_consumption.side_effect = [
        OCEAConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {"date": "2024-01-05", "valeur": 2},
                {"date": "2024-01-09", "valeur": 3},
            ],
        }),
        empty, empty, empty, empty, empty,
    ]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)

    def set_meter(installation: str) -> None:
        meter = _meter(installation)
        coordinator.async_set_updated_data(OCEAData(
            totals={},
            meters={meter.identifier: _build_meter_data(
                meter, {"unite": "m3", "consommations": []}
            )},
        ))

    set_meter("2024-01-05")
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 17, tzinfo=UTC),
            datetime(2024, 1, 17, tzinfo=UTC),
            datetime(2024, 1, 18, tzinfo=UTC),
            datetime(2024, 1, 18, tzinfo=UTC),
        ],
    ):
        for _ in range(4):
            await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
            await async_recorder_block_till_done(hass)
        set_meter("2024-01-03")
        restored = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await restored._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry.entry_id).async_remove()
        restored = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await restored._async_sync()
        await async_recorder_block_till_done(hass)

    payloads = [call.args[2] for call in client.get_dwelling_consumption.call_args_list]
    assert [(payload["debut"][:10], payload["fin"][:10]) for payload in payloads] == [
        ("2024-01-05", "2024-01-09"),
        ("2024-01-07", "2024-01-09"),
        ("2024-01-10", "2024-01-16"),
        ("2024-01-05", "2024-01-13"),
        ("2024-01-03", "2024-01-17"),
        ("2024-01-03", "2024-01-17"),
    ]
    assert restored._histories is not None
    history = restored._histories[key]
    assert history.start == date(2024, 1, 1)
    assert history.request_floor == date(2024, 1, 3)
    assert history.days == {
        date(2024, 1, 1): Decimal(1),
        date(2024, 1, 5): Decimal(2),
        date(2024, 1, 9): Decimal(3),
    }
    statistic_id = _dwelling_statistic_id("local", "EauFroide")
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2024, 1, 1, tzinfo=UTC),
        None, {statistic_id}, "hour", None, {"sum", "state"},
    )
    assert [row["sum"] for row in rows[statistic_id]] == pytest.approx([0, 1, 3, 6])


@pytest.mark.usefixtures("real_recorder")
async def test_replaced_meter_bounds_requests_and_keeps_old_statistics(
    hass: HomeAssistant,
) -> None:
    """Keep earlier values while bounding requests to each replacement device."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    old_meter = _meter("2024-01-01")
    key = _meter_statistic_id("local", old_meter)
    await history_store(hass, entry.entry_id).async_save({
        "generation": "generation",
        "meters": {key: {
            "start": "2024-01-01",
            "cursor": "2024-01-04",
            "days": {"2024-01-03": "1"},
            "recheck_cursor": "2024-01-01",
            "next_request": None,
        }},
    })
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = []

    def response(local_id, pds_id, payload):
        values = (
            [("2024-01-06", 0.5), ("2024-01-10", 2)]
            if pds_id == "pds-1" else [("2024-01-15", 4)]
        )
        readings = [
            {"date": day, "valeur": value}
            for day, value in values
            if payload["debut"][:10] <= day <= payload["fin"][:10]
        ]
        return OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": readings})

    client.get_pds_consumption.side_effect = response
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    replacement = _meter("2024-01-10")
    new_pds = _meter("2024-01-15", pds_id="pds-2")

    def set_meters(*meters: OCEAMeter) -> None:
        coordinator.async_set_updated_data(OCEAData(
            totals={},
            meters={
                meter.identifier: OCEAMeterReading(meter=meter, value=None, latest_date=None)
                for meter in meters
            },
        ))

    set_meters(replacement)
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 1, day, tzinfo=UTC) for day in (20, 22, 24)
        ],
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        set_meters(replacement, new_pds)
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        set_meters(_meter("2024-01-05"), new_pds)
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)

    calls = client.get_pds_consumption.call_args_list
    assert calls[0].args[2]["debut"].startswith("2024-01-10")
    assert next(call for call in calls if call.args[1] == "pds-2").args[2]["debut"].startswith(
        "2024-01-15"
    )
    assert any(
        call.args[1] == "pds-1" and call.args[2]["debut"].startswith("2024-01-05")
        for call in calls
    )
    old_id = _meter_statistic_id("local", old_meter)
    new_id = _meter_statistic_id("local", new_pds)
    assert old_id == _meter_statistic_id("local", replacement)
    assert old_id != new_id
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2024, 1, 1, tzinfo=UTC),
        None, {old_id, new_id}, "hour", None, {"sum", "state"},
    )
    assert [row["sum"] for row in rows[old_id]] == [0, 1, 1.5, 3.5]
    assert [row["sum"] for row in rows[new_id]] == [0, 4]


@pytest.mark.parametrize("kind", ["dwelling", "meter"])
async def test_history_rejects_response_before_requested_floor(
    hass: HomeAssistant, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Reject an out-of-range response without advancing or publishing it."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"] if kind == "dwelling" else []
    response = OCEAConsumptionSeries.from_api({
        "unite": "m3", "consommations": [{"date": "2024-01-04", "valeur": 1}]
    })
    client.get_dwelling_consumption.return_value = response
    client.get_pds_consumption.return_value = response
    meter = _meter("2024-01-05")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(
        totals={},
        meters={meter.identifier: OCEAMeterReading(meter=meter, value=None, latest_date=None)},
    ))
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 2, 1, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    key = _meter_statistic_id("local", meter) if kind == "meter" else _dwelling_statistic_id(
        "local", "EauFroide"
    )
    assert synchronizer._histories is not None
    assert synchronizer._histories[key].cursor == date(2024, 1, 5)
    assert synchronizer._histories[key].retry_count == 1
    assert not synchronizer._histories[key].days
    assert "outside the requested period" in caplog.text
    add.assert_not_called()


async def test_dwelling_cetc_rejects_non_energy_unit(hass: HomeAssistant) -> None:
    """Do not publish heating values with an incompatible unit."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = [CETC_FLUID]
    client.get_dwelling_consumption.return_value = OCEAConsumptionSeries.from_api({
        "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 1}]
    })
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 2, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    client.get_dwelling_consumption.assert_awaited_once()
    add.assert_not_called()
    assert synchronizer._histories is not None
    assert synchronizer._histories[_dwelling_statistic_id("local", CETC_FLUID)].cursor == date(
        2024, 3, 1
    )


async def test_dwelling_uses_supported_pds_fluids(hass: HomeAssistant) -> None:
    """A newly supported PDS fluid is also discovered for dwelling statistics."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["FutureWater"]
    client.get_dwelling_consumption.return_value = OCEAConsumptionSeries.from_api({
        "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 1}]
    })
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    with (
        patch.dict(PDS_FLUID_METADATA, {"FutureWater": PDS_FLUID_METADATA["EauFroide"]}),
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 2, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        assert add.call_args.args[1]["statistic_id"] == _dwelling_statistic_id(
            "local", "FutureWater"
        )
    client.get_dwelling_consumption.assert_awaited_once()


@pytest.mark.parametrize(
    "bad_response",
    [
        {"unite": "kWh", "consommations": []},
        {"unite": "m3", "consommations": [
            {"date": "2024-03-01", "valeur": 1, "consentement": False}
        ]},
        {"unite": "m3", "consommations": [
            {"date": "2024-02-29", "valeur": 1}
        ]},
    ],
)
async def test_invalid_dwelling_period_does_not_block_other_fluid(
    hass: HomeAssistant, bad_response: dict[str, object]
) -> None:
    """Reject invalid periods without advancing their cursor or blocking another series."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauChaude", "EauFroide"]
    client.get_dwelling_consumption.side_effect = [
        OCEAConsumptionSeries.from_api(bad_response),
        OCEAConsumptionSeries.from_api({
            "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 2}]
        }),
    ]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 2, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    assert synchronizer._histories is not None
    assert synchronizer._histories[_dwelling_statistic_id("local", "EauChaude")].cursor == date(
        2024, 3, 1
    )
    add.assert_called_once()
    assert add.call_args.args[1]["statistic_id"] == _dwelling_statistic_id("local", "EauFroide")


async def test_dwelling_backfill_requests_bounded_periods_across_restarts(
    hass: HomeAssistant,
) -> None:
    """Backfill in at most 90-day chunks, excluding the unconfirmed current day."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2023-10-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    client.get_dwelling_consumption.return_value = OCEAConsumptionSeries.from_api({
        "unite": "m3", "consommations": []
    })
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[datetime(2024, 3, 15, hour, tzinfo=UTC) for hour in (0, 0)],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics"),
    ):
        for _ in range(2):
            await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
    payloads = [call.args[2] for call in client.get_dwelling_consumption.call_args_list]
    assert [(payload["debut"][:10], payload["fin"][:10]) for payload in payloads] == [
        ("2023-10-01", "2023-12-29"),
        ("2023-12-30", "2024-03-14"),
    ]


async def test_backfill_splits_a_range_rejected_for_size(hass: HomeAssistant) -> None:
    """Retry a 413 period with a smaller persisted range."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local",
            "dateDebut": "2024-01-01T00:00:00Z",
            "dateFin": None,
        }]
    })
    client.get_pds_consumption.side_effect = [
        OCEAApiError("request too large", status=413),
        OCEAConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [{"date": "2024-01-03", "valeur": 0.1}],
        }),
    ]
    meter = _meter()
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter, {"unite": "m3", "consommations": []}
                )
            },
        )
    )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 4, 15, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics"),
    ):
        synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await synchronizer._async_sync()

    requests = [call.args[2] for call in client.get_pds_consumption.call_args_list]
    assert (requests[0]["debut"][:10], requests[0]["fin"][:10]) == (
        "2024-01-03",
        "2024-04-01",
    )
    assert (requests[1]["debut"][:10], requests[1]["fin"][:10]) == (
        "2024-01-03",
        "2024-02-16",
    )
    assert synchronizer._histories is not None
    assert synchronizer._histories[_meter_statistic_id("local", meter)].cursor == date(2024, 2, 17)


async def test_history_requests_respect_concurrency_limit(hass: HomeAssistant) -> None:
    """Run no more than the configured number of history requests at once."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local",
            "dateDebut": "2024-01-01T00:00:00Z",
            "dateFin": None,
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    active = 0
    maximum_active = 0
    first_batch_started = asyncio.Event()
    release_requests = asyncio.Event()

    async def request(*args: object) -> OCEAConsumptionSeries:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            first_batch_started.set()
        await release_requests.wait()
        active -= 1
        return OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []})

    client.get_pds_consumption.side_effect = request
    client.get_dwelling_consumption.side_effect = request
    meters = [_meter(pds_id=f"pds-{index}") for index in range(4)]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter, {"unite": "m3", "consommations": []}
                )
                for meter in meters
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        return_value=datetime(2024, 3, 2, tzinfo=UTC),
    ):
        sync_task = asyncio.create_task(synchronizer._async_sync())
        try:
            await asyncio.wait_for(first_batch_started.wait(), timeout=1)
            assert active == maximum_active == 3
        finally:
            release_requests.set()
        await sync_task

    assert maximum_active == 3


@pytest.mark.parametrize(
    "response",
    [
        {"unite": "m3", "consommations": [{"date": "2024-01-03"}]},
        {"unite": "litres", "consommations": []},
        {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": -1}]},
        {
            "unite": "m3",
            "consommations": [
                {"date": "2024-01-03", "valeur": 1},
                {"date": "2024-01-03", "valeur": 1},
            ],
        },
    ],
)
def test_reject_invalid_history_response(response: dict[str, object]) -> None:
    with pytest.raises(OCEAApiError):
        _parse_daily_response(OCEAConsumptionSeries.from_api(response), "m3")


def test_history_uses_paris_day_for_typed_timestamps() -> None:
    """A timestamp crossing midnight is assigned to its OCEA calendar day."""
    series = OCEAConsumptionSeries.from_api({
        "unite": "m3",
        "consommations": [{"date": "2024-03-01T23:30:00Z", "valeur": "0,2"}],
    })
    assert _parse_daily_response(series, "m3") == (
        OCEADailyConsumption(date(2024, 3, 2), Decimal("0.2")),
    )


def test_invalid_partial_series_is_not_reused_for_history() -> None:
    """Partial totals remain available but cannot advance the history cursor."""
    reading = _build_meter_data(_meter(), {
        "unite": "m3",
        "consommations": [
            {"date": "2024-03-01", "valeur": "0,2"},
            {"date": "2024-03-02", "valeur": "invalid"},
        ],
    })
    assert reading.value == Decimal("0.2")
    assert reading.daily_consumptions is None


def test_daily_statistics_use_ocea_time_zone_across_dst() -> None:
    records = _daily_statistics(
        date(2024, 3, 30),
        {date(2024, 3, 30): Decimal("0.1"), date(2024, 3, 31): Decimal("0.2")},
    )
    assert records[1]["start"] == datetime(2024, 3, 30, 22, tzinfo=UTC)
    assert records[2]["start"] == datetime(2024, 3, 31, 21, tzinfo=UTC)
    assert records[2]["sum"] == pytest.approx(0.3)


def test_daily_statistics_use_last_hour_across_winter_time_change() -> None:
    records = _daily_statistics(
        date(2024, 10, 26),
        {date(2024, 10, 26): Decimal("0.1"), date(2024, 10, 27): Decimal("0.2")},
    )
    assert records[1]["start"] == datetime(2024, 10, 26, 21, tzinfo=UTC)
    assert records[2]["start"] == datetime(2024, 10, 27, 22, tzinfo=UTC)
    assert (records[2]["start"] - records[1]["start"]).total_seconds() == 25 * 3600


@pytest.mark.usefixtures("real_recorder")
@pytest.mark.parametrize(
    ("first_day", "expected_hours"),
    [
        (
            date(2024, 3, 30),
            [datetime(2024, 3, 30, 22, tzinfo=UTC),
             datetime(2024, 3, 31, 21, tzinfo=UTC),
             datetime(2024, 4, 1, 21, tzinfo=UTC)],
        ),
        (
            date(2024, 10, 26),
            [datetime(2024, 10, 26, 21, tzinfo=UTC),
             datetime(2024, 10, 27, 22, tzinfo=UTC),
             datetime(2024, 10, 28, 22, tzinfo=UTC)],
        ),
        (
            date(2024, 12, 31),
            [datetime(2024, 12, 31, 22, tzinfo=UTC),
             datetime(2025, 1, 1, 22, tzinfo=UTC),
             datetime(2025, 1, 2, 22, tzinfo=UTC)],
        ),
    ],
)
async def test_calendar_boundaries_recorder_correction_and_cache_restore(
    hass: HomeAssistant, first_day: date, expected_hours: list[datetime]
) -> None:
    """Keep Paris daily statistics and corrected sums through cache loss."""
    await hass.config.async_set_time_zone("America/Los_Angeles")
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{
            "logementId": "local",
            "dateDebut": f"{first_day.isoformat()}T00:00:00+01:00",
            "dateFin": None,
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    days = [first_day + timedelta(days=index) for index in range(3)]

    def response(values: list[float]) -> OCEAConsumptionSeries:
        return OCEAConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {"date": f"{day.isoformat()}T00:00:00", "valeur": value}
                for day, value in zip(days, values, strict=True)
            ],
        })

    client.get_dwelling_consumption.side_effect = [
        response([1, 2, 3]),
        response([1.5, 2, 3]),
        OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []}),
    ]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    current_day = first_day + timedelta(days=3)
    now = datetime.combine(current_day, datetime.min.time(), tzinfo=UTC)
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[now, now + timedelta(hours=5), now + timedelta(hours=10)],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry.entry_id).async_remove()
        restored = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await restored._async_sync()
        await async_recorder_block_till_done(hass)

    payloads = [call.args[2] for call in client.get_dwelling_consumption.call_args_list]
    assert payloads == [{
        "debut": f"{first_day.isoformat()}T00:00:00.000Z",
        "fin": f"{days[-1].isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }] * 3
    statistic_id = _dwelling_statistic_id("local", "EauFroide")
    assert restored._histories is not None
    history = restored._histories[_dwelling_statistic_id("local", "EauFroide")]
    assert history.days == dict(zip(days, map(Decimal, ("1.5", "2", "3")), strict=True))
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, expected_hours[0] - timedelta(hours=2),
        None, {statistic_id}, "hour", None, {"sum", "state"},
    )
    assert set(rows) == {statistic_id}
    records = rows[statistic_id]
    assert [row["start"] for row in records] == [
        (expected_hours[0] - timedelta(hours=1)).timestamp(),
        *(hour.timestamp() for hour in expected_hours),
    ]
    assert [row["state"] for row in records] == pytest.approx([0, 1.5, 2, 3])
    assert [row["sum"] for row in records] == pytest.approx([0, 1.5, 3.5, 6.5])


@pytest.mark.usefixtures("real_recorder")
async def test_existing_history_is_preserved_when_occupation_start_moves_forward(
    hass: HomeAssistant,
) -> None:
    """Keep earlier cached and Recorder values from the former UTC-based start."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)

    def resident(start: str) -> OCEAResident:
        return OCEAResident.from_api({
            "occupations": [{"logementId": "local", "dateDebut": start, "dateFin": None}]
        })

    client.get_resident.return_value = resident("2023-12-31T23:00:00Z")
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    client.get_dwelling_consumption.side_effect = [
        OCEAConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {"date": "2023-12-31", "valeur": 1},
                {"date": "2024-01-01", "valeur": 2},
            ],
        }),
        OCEAConsumptionSeries.from_api({"unite": "m3", "consommations": []}),
    ]
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters={}))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[datetime(2024, 1, 2, hour, tzinfo=UTC) for hour in (0, 5)],
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        client.get_resident.return_value = resident("2024-01-01T00:00:00+01:00")
        restored = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await restored._async_sync()
        await async_recorder_block_till_done(hass)

    assert restored._histories is not None
    history = restored._histories[_dwelling_statistic_id("local", "EauFroide")]
    assert history.start == date(2023, 12, 31)
    assert history.days == {date(2023, 12, 31): Decimal(1), date(2024, 1, 1): Decimal(2)}
    statistic_id = _dwelling_statistic_id("local", "EauFroide")
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2023, 12, 31, tzinfo=UTC),
        None, {statistic_id}, "hour", None, {"sum", "state"},
    )
    assert [row["sum"] for row in rows[statistic_id]] == pytest.approx([0, 1, 3])


@pytest.mark.parametrize("value", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_current_readings_reject_invalid_decimal(value: Decimal) -> None:
    """Typed daily values must still be finite and non-negative."""
    reading = OCEAMeterReading(
        meter=_meter(),
        value=None,
        latest_date=None,
        unit="m3",
        daily_consumptions=(OCEADailyConsumption(date(2024, 3, 10), value),),
    )
    with pytest.raises(OCEAApiError):
        _current_daily_readings(reading, "m3", date(2024, 3, 12))


@pytest.mark.usefixtures("real_recorder")
@pytest.mark.parametrize(
    ("fluid", "unit", "prefix", "display_label", "unit_class", "ha_unit"),
    [
        (
            "EauFroide",
            "m3",
            "cold_water",
            "Cold water",
            "volume",
            UnitOfVolume.CUBIC_METERS,
        ),
        (
            "EauChaude",
            "m3",
            "hot_water",
            "Hot water",
            "volume",
            UnitOfVolume.CUBIC_METERS,
        ),
        (
            CETC_FLUID,
            "kWh",
            "heating_energy",
            "Heating energy",
            "energy",
            UnitOfEnergy.KILO_WATT_HOUR,
        ),
    ],
)
async def test_backfill_restart_and_correction_update_external_statistics(
    hass: HomeAssistant,
    fluid: str,
    unit: str,
    prefix: str,
    display_label: str,
    unit_class: str,
    ha_unit: str,
) -> None:
    """Keep recent readings correct when earlier chunks arrive and after restart."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OCEAConsumptionSeries.from_api({
            "unite": unit,
            "consommations": [{"date": "2024-01-03", "valeur": 0.1}],
        }),
        OCEAConsumptionSeries.from_api({
            "unite": unit,
            "consommations": [{"date": "2024-03-01", "valeur": 0.4}],
        }),
    ]
    meter = _meter(fluid=fluid)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": unit, "consommations": [{"date": "2024-03-01", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 3, 2, 0, tzinfo=UTC),
            datetime(2024, 3, 2, 5, tzinfo=UTC),
        ],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)

    calls = client.get_pds_consumption.call_args_list
    assert all(call.args[:2] == ("local", "pds-1") for call in calls)
    assert calls[0].args[2]["debut"].startswith("2024-01-03")
    assert calls[0].args[2]["fin"].startswith("2024-03-01")
    assert calls[1].args[2]["debut"].startswith("2024-02-28")
    assert calls[1].args[2]["fin"].startswith("2024-03-01")
    assert synchronizer._generation is not None
    statistic_id = f"{DOMAIN}:{_history_id('local', meter)}_{prefix}_consumption"
    assert synchronizer._histories is not None
    assert synchronizer._histories[_meter_statistic_id("local", meter)].cursor == date(2024, 3, 2)
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    assert metadata[statistic_id][1]["unit_class"] == unit_class
    assert metadata[statistic_id][1]["unit_of_measurement"] == ha_unit
    assert metadata[statistic_id][1]["name"] == (
        f"OCEA - local - {meter.serial_number} - {display_label} consumption"
    )
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )
    assert [row["sum"] for row in rows[statistic_id]] == pytest.approx(
        [0, 0.1, 0.5]
    )
    daily = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "day",
        None,
        {"change"},
    )
    assert [row["change"] for row in daily[statistic_id]] == pytest.approx(
        [0.1, 0.4]
    )


@pytest.mark.parametrize("status", [429, 503])
async def test_failed_history_is_retried_without_blocking_recent_data(
    hass: HomeAssistant, status: int
) -> None:
    """Keep the backfill cursor and publish recent values after a period fails."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OCEAApiError("temporary error", status=status),
        OCEAConsumptionSeries.from_api(
            {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.2}]}
        ),
    ]
    meter = _meter()
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[
                datetime(2024, 3, 12, hour, tzinfo=UTC) for hour in (0, 5)
            ],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
        assert synchronizer._histories is not None
        history = synchronizer._histories[_meter_statistic_id("local", meter)]
        assert history.retry_count == 1
        assert history.next_request == datetime(2024, 3, 12, 0, 1, tzinfo=UTC)
        await synchronizer._async_sync()

    assert client.get_pds_consumption.call_args_list[0] == client.get_pds_consumption.call_args_list[1]
    assert [row["sum"] for row in add.call_args_list[0].args[2]] == pytest.approx([0, 0.3])
    assert [row["sum"] for row in add.call_args_list[1].args[2]] == pytest.approx([0.2, 0.5])


async def test_current_month_backfill_reuses_coordinator_readings(hass: HomeAssistant) -> None:
    """Use coordinator readings instead of duplicating the current-month request."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    meter = _meter("2024-03-01")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    client.get_pds_consumption.assert_not_awaited()
    add.assert_called_once()


async def test_history_response_wins_over_stale_current_month_reading(
    hass: HomeAssistant,
) -> None:
    """Do not overwrite a fresher history correction with coordinator data."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    meter = _meter("2024-03-01")
    key = _meter_statistic_id("local", meter)
    await history_store(hass, entry.entry_id).async_save(
        {
            "generation": "generation",
            "meters": {
                key: {
                    "start": "2024-03-01",
                    "cursor": "2024-03-13",
                    "days": {"2024-03-10": "0.3"},
                    "recheck_cursor": "2024-03-01",
                    "next_request": None,
                }
            },
        }
    )
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OCEAConsumptionSeries.from_api(
        {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.4}]}
    )
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    assert [row["sum"] for row in add.call_args.args[2]] == [0, 0.4]


@pytest.mark.parametrize(
    "response",
    [
        {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 1}]},
        {"consommations": [{"date": "2024-03-10", "valeur": 1}]},
        {"unite": "kWh"},
        {"unite": "kWh", "consommations": [{"date": "2024-03-10", "valeur": None}]},
        {"unite": "kWh", "consommations": [{"date": "2024-03-10"}]},
        {"unite": "kWh", "consommations": [{"date": "2024-02-29", "valeur": 1}]},
        {"unite": "kWh", "consommations": [{"date": "2024-03-10", "valeur": -1}]},
        {"unite": "kWh", "consommations": [
            {"date": "2024-03-10", "valeur": 1, "consentement": False}
        ]},
        {"unite": "kWh", "consommations": [
            {"date": "2024-03-10", "valeur": 1},
            {"date": "2024-03-10", "valeur": 2},
        ]},
    ],
)
async def test_invalid_cetc_data_does_not_advance_history(
    hass: HomeAssistant, response: dict[str, object]
) -> None:
    """Reject invalid recent and historical data and retry when valid data arrives."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OCEAConsumptionSeries.from_api(
        {"unite": "m3", "consommations": []}
    )
    meter = _meter("2024-03-01", CETC_FLUID)
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(totals={}, meters={meter.identifier: _build_meter_data(meter, response)})
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
        assert synchronizer._histories is not None
        history = next(iter(synchronizer._histories.values()))
        assert history.cursor == date(2024, 3, 1)
        assert not history.days
        add.assert_not_called()
        coordinator.async_set_updated_data(OCEAData(
            totals={},
            meters={meter.identifier: _build_meter_data(meter, {
                "unite": "kWh",
                "consommations": [{"date": "2024-03-10", "valeur": 2}],
            })},
        ))
        await synchronizer._async_sync()
    assert client.get_pds_consumption.await_count == 1
    assert [row["sum"] for row in add.call_args.args[2]] == [0, 2]


async def test_meter_type_change_keeps_water_and_energy_histories_separate(
    hass: HomeAssistant,
) -> None:
    """The same PDS identifier cannot reuse water history as energy."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        for fluid, unit, value in (("EauFroide", "m3", 3), (CETC_FLUID, "kWh", 5)):
            meter = _meter("2024-03-01", fluid)
            coordinator.async_set_updated_data(OCEAData(
                totals={},
                meters={meter.identifier: _build_meter_data(meter, {
                    "unite": unit,
                    "consommations": [{"date": "2024-03-10", "valeur": value}],
                })},
            ))
            await synchronizer._async_sync()
    assert synchronizer._histories is not None
    assert len(synchronizer._histories) == 2
    assert [call.args[1]["unit_class"] for call in add.call_args_list] == ["volume", "energy"]
    assert [[row["sum"] for row in call.args[2]] for call in add.call_args_list] == [
        [0, 3], [0, 5]
    ]


async def test_invalid_meter_does_not_block_other_meters(hass: HomeAssistant) -> None:
    """A missing installation date must not prevent other PDS from syncing."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    bad_meter = _meter(None, CETC_FLUID)
    good_meter = OCEAMeter.from_api(
        {"id": "pds-2", "fluide": "EauChaude"}, {"datePose": "2024-03-01"}
    )
    readings = {
        meter.identifier: _build_meter_data(
            meter, {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]}
        )
        for meter in (bad_meter, good_meter)
    }
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OCEAData(totals={}, meters=readings))
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    add.assert_called_once()
    assert add.call_args.args[1]["name"] == (
        f"OCEA - local - {_history_id('local', good_meter)[:8]} - Hot water consumption"
    )


@pytest.mark.usefixtures("real_recorder")
async def test_missing_cache_reuses_existing_statistics_series(
    hass: HomeAssistant,
) -> None:
    """Losing the cache must rebuild data under the same Recorder ID."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OCEAConsumptionSeries.from_api(
            {
                "unite": "m3",
                "consommations": [
                    {"date": "2024-01-03", "valeur": 0.1},
                    {"date": "2024-01-20", "valeur": 0.2},
                ],
            }
        ),
        OCEAConsumptionSeries.from_api(
            {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.1}]}
        ),
    ]
    meter = _meter("2024-01-01")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        return_value=datetime(2024, 3, 12, tzinfo=UTC),
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry.entry_id).async_remove()
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)
    statistic_id = _meter_statistic_id("local", meter)
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )
    assert set(metadata) == {statistic_id}
    assert [row["sum"] for row in rows[statistic_id]] == pytest.approx(
        [0, 0.1, 0.3, 0.6]
    )


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_reinstall_restores_separate_dwelling_and_meter_statistics(
    hass: HomeAssistant,
) -> None:
    """Reinstalling one dwelling retains its statistics and leaves another intact."""
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [
            {"logementId": local_id, "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}
            for local_id in ("local-a", "local-b")
        ]
    })
    client.get_pds.side_effect = lambda local_id: [
        OCEAPdsData.from_api({"id": pds_id, "fluide": CETC_FLUID})
        for pds_id in (("shared", "second") if local_id == "local-a" else ("shared",))
    ]
    client.get_appareils.side_effect = lambda payload: [
        OCEADeviceData.from_api({"pdsId": pds_id, "datePose": "2024-01-01"})
        for pds_id in payload["pdsIds"]
    ]
    client.get_consumptions.return_value = []
    client.get_dwelling_fluids.return_value = [CETC_FLUID]

    def pds_response(local_id, pds_id, payload):
        value = 2 if pds_id == "shared" else 3
        readings = [{"date": "2024-03-10", "valeur": value}]
        if payload["debut"].startswith("2024-01"):
            readings.insert(0, {"date": "2024-01-02", "valeur": 1})
        return OCEAConsumptionSeries.from_api({"unite": "kWh", "consommations": readings})

    client.get_pds_consumption.side_effect = pds_response
    client.get_dwelling_consumption.return_value = OCEAConsumptionSeries.from_api({
        "unite": "kWh",
        "consommations": [
            {"date": "2024-01-02", "valeur": 4},
            {"date": "2024-03-10", "valeur": 5},
        ],
    })

    def new_entry(local_id: str) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            version=2,
            unique_id=local_id,
            data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: local_id},
        )

    first = new_entry("local-a")
    other = new_entry("local-b")
    first.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OCEAApiClient", return_value=client),
        patch("custom_components.ocea_smart_building.OCEAHistoryManager", autospec=True),
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
    ):
        for entry in (first, other):
            if entry is other:
                other.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await OCEAHistoryManager(
            hass, client, entry.runtime_data, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        await async_recorder_block_till_done(hass)

        assert await history_store(hass, first.entry_id).async_load() is not None
        assert (await hass.config_entries.async_remove(first.entry_id))["require_restart"] is False
        assert await history_store(hass, first.entry_id).async_load() is None

        replacement = new_entry("local-a")
        replacement.add_to_hass(hass)
        assert replacement.entry_id != first.entry_id
        assert await hass.config_entries.async_setup(replacement.entry_id)
        await OCEAHistoryManager(
            hass, client, replacement.runtime_data,
            replacement.data[CONF_LOCAL_ID], replacement.entry_id,
        )._async_sync()
        await async_recorder_block_till_done(hass)

        ids = {
            _dwelling_statistic_id(local_id, CETC_FLUID)
            for local_id in ("local-a", "local-b")
        } | {
            _meter_statistic_id(local_id, _meter(fluid=CETC_FLUID, pds_id=pds_id))
            for local_id, pds_id in (
                ("local-a", "shared"), ("local-a", "second"), ("local-b", "shared")
            )
        }
        metadata = await get_instance(hass).async_add_executor_job(
            partial(get_metadata, hass, statistic_ids=ids)
        )
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, datetime(2024, 1, 1, tzinfo=UTC),
            None, ids, "hour", None, {"sum", "state"},
        )
        assert set(metadata) == set(rows) == ids
        assert all(item[1]["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR for item in metadata.values())
        for local_id in ("local-a", "local-b"):
            assert [row["sum"] for row in rows[_dwelling_statistic_id(local_id, CETC_FLUID)]] == [0, 4, 9]
            shared_id = _meter_statistic_id(local_id, _meter(fluid=CETC_FLUID, pds_id="shared"))
            assert [row["sum"] for row in rows[shared_id]] == [0, 1, 3]
        second_id = _meter_statistic_id("local-a", _meter(fluid=CETC_FLUID, pds_id="second"))
        assert [row["sum"] for row in rows[second_id]] == [0, 1, 4]
        assert await hass.config_entries.async_unload(replacement.entry_id)
        assert await hass.config_entries.async_unload(other.entry_id)


async def test_invalid_cache_rebuilds_without_changing_statistic_id(
    hass: HomeAssistant,
) -> None:
    """Rebuild structurally invalid cache data without creating a new series."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    await history_store(hass, entry.entry_id).async_save({"generation": "bad", "meters": []})
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    meter = _meter("2024-03-01")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    assert add.call_args.args[1]["statistic_id"] == _meter_statistic_id("local", meter)


async def test_backfill_progress_advances_across_restart_without_five_hour_gate(
    hass: HomeAssistant,
) -> None:
    """Resume the next backfill chunk immediately after a synchronizer restart."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OCEAApiClient)
    client.get_resident.return_value = OCEAResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2023-10-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OCEAConsumptionSeries.from_api(
        {"unite": "m3", "consommations": []}
    )
    meter = _meter("2023-10-01")
    coordinator = OCEADataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OCEAData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[
                datetime(2024, 3, 12, 0, 0, tzinfo=UTC),
                datetime(2024, 3, 12, 4, 59, tzinfo=UTC),
                datetime(2024, 3, 12, 5, 0, tzinfo=UTC),
            ],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics"),
    ):
        await OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )._async_sync()
        restarted = OCEAHistoryManager(
            hass, client, coordinator, entry.data[CONF_LOCAL_ID], entry.entry_id
        )
        await restarted._async_sync()
        assert client.get_pds_consumption.await_count == 2
        await restarted._async_sync()
    assert client.get_pds_consumption.await_count == 3
    payloads = [call.args[2] for call in client.get_pds_consumption.call_args_list]
    assert payloads[0]["debut"].startswith("2023-10-01")
    assert payloads[0]["fin"].startswith("2023-12-29")
    assert payloads[1]["debut"].startswith("2023-12-30")
    assert payloads[1]["fin"].startswith("2024-03-11")


def test_history_store_keeps_entry_scoped_storage_key(hass: HomeAssistant) -> None:
    """Keep the persisted path unchanged and separate for each config entry."""
    first = history_store(hass, "entry-1")
    assert first.key == f"{DOMAIN}_history.entry-1"
    assert first.version == 1
    assert history_store(hass, "entry-2").key != first.key


def test_manager_uses_injected_local_id_and_storage_key(hass: HomeAssistant) -> None:
    """Use explicit dependencies without requiring a config entry."""
    manager = OCEAHistoryManager(
        hass, AsyncMock(), AsyncMock(), "injected", "custom-key"
    )
    assert manager.local_id == "injected"
    assert manager.store.key == f"{DOMAIN}_history.custom-key"
