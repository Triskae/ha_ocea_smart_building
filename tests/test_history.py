"""Tests for dated Ocea external statistics."""

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
from homeassistant.const import UnitOfEnergy, UnitOfVolume
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
    OceaDailyConsumption,
    OceaData,
    OceaDataUpdateCoordinator,
    OceaMeterReading,
)
from custom_components.ocea_smart_building.coordinator import (
    _build_meter_data as build_meter_data,
)
from custom_components.ocea_smart_building.history import (
    OceaHistorySynchronizer,
    _current_daily_readings,
    _daily_statistics,
    _dwelling_history_id,
    _dwelling_statistic_id,
    _history_id,
    _history_start,
    _meter_statistic_id,
    _occupation_start,
    _parse_daily_response,
    history_store,
)
from custom_components.ocea_smart_building.ocea import (
    OceaApiClient,
    OceaApiError,
    OceaMeter,
    OceaResident,
)
from custom_components.ocea_smart_building.ocea.models import (
    OceaConsumptionResponse,
    OceaConsumptionSeries,
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
    meter: OceaMeter, response: OceaConsumptionResponse
) -> OceaMeterReading:
    """Build readings from a synthetic API response at the client boundary."""
    return build_meter_data(meter, OceaConsumptionSeries.from_api(response))


def _meter(
    installation_date: str | None = "2024-01-03T00:00:00Z",
    fluid: str = "EauFroide",
    pds_id: str = "pds-1",
) -> OceaMeter:
    return OceaMeter.from_api(
        {"id": pds_id, "fluide": fluid},
        {"datePose": installation_date, "numeroSerie": "example"},
    )


def test_occupation_and_meter_dates_are_distinct() -> None:
    resident = OceaResident.from_api({
        "occupations": [
            {"logementId": "other", "dateDebut": "2022-01-01T00:00:00Z", "dateFin": None},
            {"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None},
        ]
    })
    start = _occupation_start(resident, "local", date(2024, 3, 1))
    assert start == date(2024, 1, 1)
    assert _history_start(start, _meter()) == date(2024, 1, 3)
    assert _history_start(date(2024, 1, 4), _meter()) == date(2024, 1, 4)
    with pytest.raises(OceaApiError):
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
    resident = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": start, "dateFin": end}]
    })
    assert _occupation_start(resident, "local", today) == expected_start


def test_history_identity_uses_only_dwelling_pds_and_fluid() -> None:
    """Keep the Recorder identity stable across cache and configuration changes."""
    identity = "local\0pds-1\0EauFroide"
    expected = sha256(identity.encode()).hexdigest()
    meter = _meter()
    assert _history_id("local", meter) == expected
    assert _meter_statistic_id("local", meter) == f"{DOMAIN}:cold_water_{expected}"
    assert _meter_statistic_id("local", _meter(fluid="EauChaude")) != _meter_statistic_id(
        "local", meter
    )
    assert _meter_statistic_id("local", _meter(fluid=CETC_FLUID)) != _meter_statistic_id(
        "local", meter
    )
    assert _meter_statistic_id("other", meter) != _meter_statistic_id("local", meter)
    assert _meter_statistic_id("local", _meter(pds_id="pds-2")) != _meter_statistic_id(
        "local", meter
    )
    assert _meter_statistic_id("local", _meter("2025-01-01", pds_id="pds-1")) == (
        _meter_statistic_id("local", meter)
    )
    dwelling_hash = sha256(b"local\0EauFroide").hexdigest()
    assert _dwelling_statistic_id("local", "EauFroide") == f"{DOMAIN}:cold_water_{dwelling_hash}"
    assert _dwelling_statistic_id("local", "EauFroide") != _meter_statistic_id("local", meter)
    assert _dwelling_statistic_id("local", "EauFroide") != _dwelling_statistic_id(
        "other", "EauFroide"
    )
    assert _dwelling_statistic_id("local", "EauFroide") != _dwelling_statistic_id(
        "local", "EauChaude"
    )


@pytest.mark.usefixtures("real_recorder")
async def test_dwelling_history_backfill_correction_and_restore(
    hass: HomeAssistant,
) -> None:
    """Keep water and heating aggregates correct across restart and cache loss."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide", "EauChaude", CETC_FLUID, "Other"]
    calls: dict[str, int] = {}

    def series(local_id, fluid, payload):
        assert local_id == "local"
        count = calls.get(fluid, 0)
        calls[fluid] = count + 1
        values = [1, 2] if count == 0 else [1.5, 2] if count == 1 else [1.5]
        return OceaConsumptionSeries.from_api({
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
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 3, 3, hour, tzinfo=UTC) for hour in (0, 4, 5, 10)
        ],
    ):
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        await async_recorder_block_till_done(hass)
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        assert client.get_dwelling_consumption.await_count == 6
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry).async_remove()
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
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
        assert metadata[statistic_id][1]["name"] == f"OCEA - local - {label}"
        assert [row["sum"] for row in rows[statistic_id]] == pytest.approx([0, 1.5, 3.5])
        assert _dwelling_history_id("local", fluid) != _history_id("local", _meter(fluid=fluid))


async def test_dwelling_cetc_rejects_non_energy_unit(hass: HomeAssistant) -> None:
    """Do not publish heating values with an incompatible unit."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = [CETC_FLUID]
    client.get_dwelling_consumption.return_value = OceaConsumptionSeries.from_api({
        "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 1}]
    })
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    assert synchronizer._histories[_dwelling_history_id("local", CETC_FLUID)].cursor == date(
        2024, 3, 1
    )


async def test_dwelling_uses_supported_pds_fluids(hass: HomeAssistant) -> None:
    """A newly supported PDS fluid is also discovered for dwelling statistics."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["FutureWater"]
    client.get_dwelling_consumption.return_value = OceaConsumptionSeries.from_api({
        "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 1}]
    })
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    with (
        patch.dict(PDS_FLUID_METADATA, {"FutureWater": PDS_FLUID_METADATA["EauFroide"]}),
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 2, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauChaude", "EauFroide"]
    client.get_dwelling_consumption.side_effect = [
        OceaConsumptionSeries.from_api(bad_response),
        OceaConsumptionSeries.from_api({
            "unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 2}]
        }),
    ]
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 2, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    assert synchronizer._histories is not None
    assert synchronizer._histories[_dwelling_history_id("local", "EauChaude")].cursor == date(
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2023-10-01T00:00:00Z", "dateFin": None}]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    client.get_dwelling_consumption.return_value = OceaConsumptionSeries.from_api({
        "unite": "m3", "consommations": []
    })
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[datetime(2024, 3, 15, hour, tzinfo=UTC) for hour in (0, 0)],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics"),
    ):
        for _ in range(2):
            await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
    payloads = [call.args[2] for call in client.get_dwelling_consumption.call_args_list]
    assert [(payload["debut"][:10], payload["fin"][:10]) for payload in payloads] == [
        ("2023-10-01", "2023-12-29"),
        ("2023-12-30", "2024-03-14"),
    ]


async def test_backfill_splits_a_range_rejected_for_size(hass: HomeAssistant) -> None:
    """Retry a 413 period with a smaller persisted range."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{
            "logementId": "local",
            "dateDebut": "2024-01-01T00:00:00Z",
            "dateFin": None,
        }]
    })
    client.get_pds_consumption.side_effect = [
        OceaApiError("request too large", status=413),
        OceaConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [{"date": "2024-01-03", "valeur": 0.1}],
        }),
    ]
    meter = _meter()
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
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
        synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    assert synchronizer._histories[_history_id("local", meter)].cursor == date(2024, 2, 17)


async def test_history_requests_respect_concurrency_limit(hass: HomeAssistant) -> None:
    """Run no more than the configured number of history requests at once."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
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

    async def request(*args: object) -> OceaConsumptionSeries:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            first_batch_started.set()
        await release_requests.wait()
        active -= 1
        return OceaConsumptionSeries.from_api({"unite": "m3", "consommations": []})

    client.get_pds_consumption.side_effect = request
    client.get_dwelling_consumption.side_effect = request
    meters = [_meter(pds_id=f"pds-{index}") for index in range(4)]
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter, {"unite": "m3", "consommations": []}
                )
                for meter in meters
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    with pytest.raises(OceaApiError):
        _parse_daily_response(OceaConsumptionSeries.from_api(response), "m3")


def test_history_uses_paris_day_for_typed_timestamps() -> None:
    """A timestamp crossing midnight is assigned to its Ocea calendar day."""
    series = OceaConsumptionSeries.from_api({
        "unite": "m3",
        "consommations": [{"date": "2024-03-01T23:30:00Z", "valeur": "0,2"}],
    })
    assert _parse_daily_response(series, "m3") == (
        OceaDailyConsumption(date(2024, 3, 2), Decimal("0.2")),
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{
            "logementId": "local",
            "dateDebut": f"{first_day.isoformat()}T00:00:00+01:00",
            "dateFin": None,
        }]
    })
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    days = [first_day + timedelta(days=index) for index in range(3)]

    def response(values: list[float]) -> OceaConsumptionSeries:
        return OceaConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {"date": f"{day.isoformat()}T00:00:00", "valeur": value}
                for day, value in zip(days, values, strict=True)
            ],
        })

    client.get_dwelling_consumption.side_effect = [
        response([1, 2, 3]),
        response([1.5, 2, 3]),
        OceaConsumptionSeries.from_api({"unite": "m3", "consommations": []}),
    ]
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    current_day = first_day + timedelta(days=3)
    now = datetime.combine(current_day, datetime.min.time(), tzinfo=UTC)
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[now, now + timedelta(hours=5), now + timedelta(hours=10)],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry).async_remove()
        restored = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    history = restored._histories[_dwelling_history_id("local", "EauFroide")]
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
    client = AsyncMock(spec=OceaApiClient)

    def resident(start: str) -> OceaResident:
        return OceaResident.from_api({
            "occupations": [{"logementId": "local", "dateDebut": start, "dateFin": None}]
        })

    client.get_resident.return_value = resident("2023-12-31T23:00:00Z")
    client.get_dwelling_fluids.return_value = ["EauFroide"]
    client.get_dwelling_consumption.side_effect = [
        OceaConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {"date": "2023-12-31", "valeur": 1},
                {"date": "2024-01-01", "valeur": 2},
            ],
        }),
        OceaConsumptionSeries.from_api({"unite": "m3", "consommations": []}),
    ]
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters={}))
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[datetime(2024, 1, 2, hour, tzinfo=UTC) for hour in (0, 5)],
    ):
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        await async_recorder_block_till_done(hass)
        client.get_resident.return_value = resident("2024-01-01T00:00:00+01:00")
        restored = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await restored._async_sync()
        await async_recorder_block_till_done(hass)

    assert restored._histories is not None
    history = restored._histories[_dwelling_history_id("local", "EauFroide")]
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
    reading = OceaMeterReading(
        meter=_meter(),
        value=None,
        latest_date=None,
        unit="m3",
        daily_consumptions=(OceaDailyConsumption(date(2024, 3, 10), value),),
    )
    with pytest.raises(OceaApiError):
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OceaConsumptionSeries.from_api({
            "unite": unit,
            "consommations": [{"date": "2024-01-03", "valeur": 0.1}],
        }),
        OceaConsumptionSeries.from_api({
            "unite": unit,
            "consommations": [{"date": "2024-03-01", "valeur": 0.4}],
        }),
    ]
    meter = _meter(fluid=fluid)
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": unit, "consommations": [{"date": "2024-03-01", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 3, 2, 0, tzinfo=UTC),
            datetime(2024, 3, 2, 5, tzinfo=UTC),
        ],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)

    calls = client.get_pds_consumption.call_args_list
    assert all(call.args[:2] == ("local", "pds-1") for call in calls)
    assert calls[0].args[2]["debut"].startswith("2024-01-03")
    assert calls[0].args[2]["fin"].startswith("2024-03-01")
    assert calls[1].args[2]["debut"].startswith("2024-02-28")
    assert calls[1].args[2]["fin"].startswith("2024-03-01")
    assert synchronizer._generation is not None
    statistic_id = f"{DOMAIN}:{prefix}_{_history_id('local', meter)}"
    assert synchronizer._histories is not None
    assert synchronizer._histories[_history_id("local", meter)].cursor == date(2024, 3, 2)
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    assert metadata[statistic_id][1]["unit_class"] == unit_class
    assert metadata[statistic_id][1]["unit_of_measurement"] == ha_unit
    assert metadata[statistic_id][1]["name"] == (
        f"OCEA - local - {display_label} - {meter.serial_number}"
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OceaApiError("temporary error", status=status),
        OceaConsumptionSeries.from_api(
            {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.2}]}
        ),
    ]
    meter = _meter()
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
        history = synchronizer._histories[_history_id("local", meter)]
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    meter = _meter("2024-03-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    key = _history_id("local", meter)
    await history_store(hass, entry).async_save(
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api(
        {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.4}]}
    )
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api(
        {"unite": "m3", "consommations": []}
    )
    meter = _meter("2024-03-01", CETC_FLUID)
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(totals={}, meters={meter.identifier: _build_meter_data(meter, response)})
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
        coordinator.async_set_updated_data(OceaData(
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        for fluid, unit, value in (("EauFroide", "m3", 3), (CETC_FLUID, "kWh", 5)):
            meter = _meter("2024-03-01", fluid)
            coordinator.async_set_updated_data(OceaData(
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    bad_meter = _meter(None, CETC_FLUID)
    good_meter = OceaMeter.from_api(
        {"id": "pds-2", "fluide": "EauChaude"}, {"datePose": "2024-03-01"}
    )
    readings = {
        meter.identifier: _build_meter_data(
            meter, {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]}
        )
        for meter in (bad_meter, good_meter)
    }
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters=readings))
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
        f"OCEA - local - Hot water - {_history_id('local', good_meter)[:8]}"
    )


@pytest.mark.usefixtures("real_recorder")
async def test_missing_cache_reuses_existing_statistics_series(
    hass: HomeAssistant,
) -> None:
    """Losing the cache must rebuild data under the same Recorder ID."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OceaConsumptionSeries.from_api(
            {
                "unite": "m3",
                "consommations": [
                    {"date": "2024-01-03", "valeur": 0.1},
                    {"date": "2024-01-20", "valeur": 0.2},
                ],
            }
        ),
        OceaConsumptionSeries.from_api(
            {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.1}]}
        ),
    ]
    meter = _meter("2024-01-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
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
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        await async_recorder_block_till_done(hass)
        await history_store(hass, entry).async_remove()
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
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


async def test_invalid_cache_rebuilds_without_changing_statistic_id(
    hass: HomeAssistant,
) -> None:
    """Rebuild structurally invalid cache data without creating a new series."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    await history_store(hass, entry).async_save({"generation": "bad", "meters": []})
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01T00:00:00Z", "dateFin": None}]
    })
    meter = _meter("2024-03-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
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
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2023-10-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api(
        {"unite": "m3", "consommations": []}
    )
    meter = _meter("2023-10-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
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
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        restarted = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await restarted._async_sync()
        assert client.get_pds_consumption.await_count == 2
        await restarted._async_sync()
    assert client.get_pds_consumption.await_count == 3
    payloads = [call.args[2] for call in client.get_pds_consumption.call_args_list]
    assert payloads[0]["debut"].startswith("2023-10-01")
    assert payloads[0]["fin"].startswith("2023-12-29")
    assert payloads[1]["debut"].startswith("2023-12-30")
    assert payloads[1]["fin"].startswith("2024-03-11")
