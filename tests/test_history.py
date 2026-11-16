"""Tests for dated Ocea external statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    _history_id,
    _history_start,
    _occupation_start,
    _parse_daily_response,
    _statistic_id,
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


def test_history_identity_uses_only_dwelling_pds_and_fluid() -> None:
    """Keep the Recorder identity stable across cache and configuration changes."""
    identity = "local\0pds-1\0EauFroide"
    expected = sha256(identity.encode()).hexdigest()
    meter = _meter()
    assert _history_id("local", meter) == expected
    assert _statistic_id("local", meter) == f"{DOMAIN}:cold_water_{expected}"
    assert _statistic_id("local", _meter(fluid="EauChaude")) != _statistic_id(
        "local", meter
    )
    assert _statistic_id("local", _meter(fluid=CETC_FLUID)) != _statistic_id(
        "local", meter
    )
    assert _statistic_id("other", meter) != _statistic_id("local", meter)
    assert _statistic_id("local", _meter(pds_id="pds-2")) != _statistic_id(
        "local", meter
    )
    assert _statistic_id("local", _meter("2025-01-01", pds_id="pds-1")) == (
        _statistic_id("local", meter)
    )


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
        OceaConsumptionSeries.from_api(response)
        for response in [
            {"unite": unit, "consommations": [{"date": "2024-01-03", "valeur": 0.1}]},
            {"unite": unit, "consommations": [{"date": "2024-02-02", "valeur": 0.2}]},
            {"unite": unit, "consommations": [{"date": "2024-01-03", "valeur": 0.15}]},
        ]
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
            datetime(2024, 3, 2, hour, tzinfo=UTC) for hour in (0, 5, 10)
        ],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        coordinator.async_set_updated_data(
            OceaData(
                totals={},
                meters={
                    meter.identifier: _build_meter_data(
                        meter,
                        {"unite": unit, "consommations": [
                            {"date": "2024-03-01", "valeur": 0.4}
                        ]},
                    )
                },
            )
        )
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)

    calls = client.get_pds_consumption.call_args_list
    assert all(call.args[:2] == ("local", "pds-1") for call in calls)
    assert calls[0].args[2]["debut"].startswith("2024-01-03")
    assert calls[0].args[2]["fin"].startswith("2024-02-01")
    assert calls[1].args[2]["debut"].startswith("2024-02-02")
    assert calls[2].args[2]["debut"].startswith("2024-01-03")
    assert calls[2].args[2]["fin"].startswith("2024-02-01")
    assert synchronizer._generation is not None
    statistic_id = f"{DOMAIN}:{prefix}_{_history_id('local', meter)}"
    assert synchronizer._histories is not None
    assert synchronizer._histories[_history_id("local", meter)].recheck_cursor == date(
        2024, 2, 2
    )
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    assert metadata[statistic_id][1]["unit_class"] == unit_class
    assert metadata[statistic_id][1]["unit_of_measurement"] == ha_unit
    assert metadata[statistic_id][1]["name"] == (
        f"OCEA - {display_label} - {meter.serial_number}"
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
        [0, 0.15, 0.35, 0.75]
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
        [0.15, 0.2, 0.4]
    )


async def test_failed_history_is_retried_without_blocking_recent_data(
    hass: HomeAssistant,
) -> None:
    """Keep the backfill cursor and publish recent values after a period fails."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.side_effect = [
        OceaApiError("error"),
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
        f"OCEA - Hot water - {_history_id('local', good_meter)[:8]}"
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
    statistic_id = _statistic_id("local", meter)
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
    assert add.call_args.args[1]["statistic_id"] == _statistic_id("local", meter)


async def test_backfill_request_limit_survives_restart(hass: HomeAssistant) -> None:
    """Manual refreshes and restarts do not bypass the five-hour request limit."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = OceaResident.from_api({
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None}]
    })
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api(
        {"unite": "m3", "consommations": []}
    )
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
        assert client.get_pds_consumption.await_count == 1
        await restarted._async_sync()
    assert client.get_pds_consumption.await_count == 2
