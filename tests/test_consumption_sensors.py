"""Tests for Ocea consumption sensors."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.typing import UNDEFINED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaData,
    OceaDataUpdateCoordinator,
    OceaMeterReading,
    _as_update_error,
    _build_meter_data,
    _current_month_payload,
)
from custom_components.ocea_smart_building.ocea import (
    OceaApiClient,
    OceaApiError,
    OceaAuthError,
    OceaMeter,
)
from custom_components.ocea_smart_building.ocea.models import (
    OceaConsumptionSeries,
    OceaDashboardData,
    OceaDeviceData,
    OceaPdsData,
)
from custom_components.ocea_smart_building.sensor import (
    SENSOR_TYPES,
    OceaMeterSensor,
)


def _coordinator(hass: HomeAssistant, client: Mock) -> OceaDataUpdateCoordinator:
    """Create a coordinator bound to an actual config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local-123"})
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    assert coordinator.config_entry is entry
    return coordinator


def test_leak_sensor_description_is_water_measurement() -> None:
    """Test estimated leaks use the standard water measurement contract."""
    leak = next(description for description in SENSOR_TYPES if description.key == "fuite")

    assert leak.translation_key == "fuite"
    assert leak.native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert leak.device_class is SensorDeviceClass.WATER
    assert leak.state_class is SensorStateClass.MEASUREMENT


def test_meter_data_builder_aggregates_one_meter_response() -> None:
    """Test meter response parsing is isolated from coordinator I/O."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    )

    assert _build_meter_data(
        meter,
        OceaConsumptionSeries.from_api({
            "consommations": [
                {"date": "2026-09-01", "valeur": "0,321"},
                {"date": "2026-09-15", "valeur": 0.654},
            ]
        }),
    ).value == Decimal("0.975")
    assert _build_meter_data(
        meter,
        OceaConsumptionSeries.from_api(
            {"consommations": [{"date": "2026-09-15", "valeur": 0.654}]}
        ),
    ).latest_date == date(2026, 9, 15)


def test_current_month_payload_uses_requested_calendar_date() -> None:
    """Test the API payload represents the Ocea calendar month."""
    assert _current_month_payload(datetime(2026, 9, 16, tzinfo=timezone.utc)) == {
        "debut": "2026-09-01T00:00:00.000Z",
        "fin": "2026-09-16T23:59:59.999Z",
        "granularity": "Day",
    }


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        (datetime(2023, 12, 31, 23, tzinfo=timezone.utc), "2024-01-01", "2024-01-01"),
        (datetime(2024, 2, 29, 22, tzinfo=timezone.utc), "2024-02-01", "2024-02-29"),
        (datetime(2024, 2, 29, 23, tzinfo=timezone.utc), "2024-03-01", "2024-03-01"),
        (datetime(2024, 3, 30, 23, tzinfo=timezone.utc), "2024-03-01", "2024-03-31"),
        (datetime(2024, 10, 26, 22, tzinfo=timezone.utc), "2024-10-01", "2024-10-27"),
    ],
)
def test_current_month_payload_at_paris_calendar_boundaries(
    now: datetime, start: str, end: str
) -> None:
    """The request follows Paris dates regardless of the UTC calendar day."""
    assert _current_month_payload(now) == {
        "debut": f"{start}T00:00:00.000Z",
        "fin": f"{end}T23:59:59.999Z",
        "granularity": "Day",
    }


@pytest.mark.parametrize("error", [TimeoutError("secret-url"), RuntimeError("secret-url")])
def test_coordinator_error_does_not_expose_exception_details(error: Exception) -> None:
    """Network and unexpected exception details may contain credentials."""
    assert "secret-url" not in str(_as_update_error(error))


async def test_coordinator_uses_ocea_timezone(
    hass: HomeAssistant,
) -> None:
    """Test current-month boundaries follow Ocea's Paris calendar."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-cold-1", "fluide": "EauFroide"}),
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api({"consommations": []})
    coordinator = _coordinator(hass, client)

    with patch(
        "custom_components.ocea_smart_building.coordinator.dt_util.now",
        return_value=datetime(2026, 9, 30, 18, tzinfo=ZoneInfo("America/Los_Angeles")),
    ):
        await coordinator._async_setup()
        await coordinator._async_update_data()

    client.get_pds.assert_awaited_once_with("local-123")
    client.get_consumptions.assert_awaited_once_with("local-123")
    assert client.get_pds_consumption.call_args.args[:2] == ("local-123", "pds-cold-1")
    payload = client.get_pds_consumption.call_args.args[2]
    assert payload["debut"] == "2026-10-01T00:00:00.000Z"
    assert payload["fin"] == "2026-10-01T23:59:59.999Z"


async def test_coordinator_normalizes_cetc_consumption(hass: HomeAssistant) -> None:
    """Test the Cetc API code is normalized for the heating sensor."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = [
        OceaDashboardData.from_api({"fluide": "EauFroide", "valeur": "12,34"}),
        OceaDashboardData.from_api({"fluide": "EauChaude", "valeur": "5,67"}),
        OceaDashboardData.from_api({"fluide": "Cetc", "valeur": "89,01"}),
    ]
    client.get_pds.return_value = []
    client.get_appareils.return_value = []
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals == {
        "eau_froide": Decimal("12.34"),
        "eau_chaude": Decimal("5.67"),
        "cetc": Decimal("89.01"),
    }
    assert data.meters == {}


def test_unsupported_fluid_does_not_match_cetc(caplog: pytest.LogCaptureFixture) -> None:
    """Only the confirmed API spelling maps to the existing CETC sensor key."""
    data = [
        OceaDashboardData.from_api({"fluide": "CETC", "valeur": "99"}),
        OceaDashboardData.from_api({"fluide": "Cetc", "valeur": "1,5"}),
        OceaDashboardData.from_api({"fluide": "EauFroide", "valeur": "private-value"}),
    ]
    with caplog.at_level(logging.WARNING):
        totals = OceaDataUpdateCoordinator._aggregate_totals(data)
    assert totals == {"cetc": Decimal("1.5")}
    assert "Ignoring unsupported dashboard fluid" in caplog.text
    assert "Ignoring invalid dashboard consumption value" in caplog.text
    assert "private-value" not in caplog.text


async def test_coordinator_keeps_consumption_separate_per_pds(
    hass: HomeAssistant,
) -> None:
    """Test two points of the same fluid produce independent meter data."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = [
        OceaDashboardData.from_api({"fluide": "EauFroide", "valeur": "1,14"}),
    ]
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-cold-1", "fluide": "EauFroide", "emplacement": "Cuisine"}),
        OceaPdsData.from_api({"id": "pds-cold-2", "fluide": "EauFroide", "emplacement": "Salle de bain"}),
    ]
    client.get_appareils.return_value = [
        OceaDeviceData.from_api({"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"}),
        OceaDeviceData.from_api({"pdsId": "pds-cold-2", "numeroSerie": "SERIAL-2"}),
    ]
    client.get_pds_consumption.side_effect = [
        OceaConsumptionSeries.from_api({"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.321}]}),
        OceaConsumptionSeries.from_api({"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.654}]}),
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == Decimal("1.14")
    assert data.meters["pds-cold-1"].meter.pds.location == "Cuisine"
    assert data.meters["pds-cold-1"].meter.serial_number == "SERIAL-1"
    assert data.meters["pds-cold-1"].value == Decimal("0.321")
    assert data.meters["pds-cold-2"].meter.pds.location == "Salle de bain"
    assert data.meters["pds-cold-2"].meter.serial_number == "SERIAL-2"
    assert data.meters["pds-cold-2"].value == Decimal("0.654")


async def test_coordinator_ignores_metadata_for_unsupported_points(
    hass: HomeAssistant,
) -> None:
    """Unselected PDS and devices do not fail validation of supported meters."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "water", "fluide": "EauFroide"}),
        OceaPdsData.from_api({"id": "other", "fluide": "Inconnu", "fce": "unused"}),
    ]
    client.get_appareils.return_value = [
        OceaDeviceData.from_api({"pdsId": "other", "numeroSerie": 42}),
    ]
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api({})
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert set(data.meters) == {"water"}
    client.get_appareils.assert_awaited_once_with({"pdsIds": ["water"]})


def test_meter_data_builder_keeps_latest_leak_estimate() -> None:
    """Test the latest leak estimate is retained from the daily response."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = _build_meter_data(
        meter,
        OceaConsumptionSeries.from_api({
            "unite": "m3",
            "consommations": [
                {
                    "date": "2026-09-01",
                    "valeur": 0.1,
                    "fuiteEstimee": 0.0,
                    "consentement": True,
                    "type": "ConsommationEauResponse",
                },
                {
                    "date": "2026-09-15",
                    "valeur": 0.2,
                    "fuiteEstimee": 0.015,
                    "consentement": True,
                    "type": "ConsommationEauResponse",
                },
            ],
        }),
    )

    assert reading.value == Decimal("0.3")
    assert reading.estimated_leak == Decimal("0.015")
    assert reading.unit == "m3"
    assert reading.latest_date == date(2026, 9, 15)


def test_meter_data_builder_normalizes_string_leak_estimate() -> None:
    """Test string estimates and comma decimals are exposed as numbers."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = _build_meter_data(
        meter,
        OceaConsumptionSeries.from_api({
            "consommations": [
                {
                    "date": "2026-09-15",
                    "valeur": "0,321",
                    "fuiteEstimee": "0,015",
                    "type": "ConsommationEauResponse",
                }
            ],
        }),
    )

    assert reading.value == Decimal("0.321")
    assert reading.estimated_leak == Decimal("0.015")


@pytest.mark.parametrize("kind", [None, "FutureResponse", "ConsommationEauResponse"])
def test_meter_data_builder_preserves_leaks_for_unclassified_items(kind: str | None) -> None:
    """A missing or new discriminator does not discard an existing leak estimate."""
    meter = OceaMeter.from_api({"id": "pds-1", "fluide": "EauFroide"}, None)
    response = {
        "consommations": [
            {"type": kind, "date": "2026-09-15", "valeur": 1, "fuiteEstimee": 0.2}
        ]
    }

    assert _build_meter_data(meter, OceaConsumptionSeries.from_api(response)).estimated_leak == Decimal("0.2")


def test_invalid_readings_produce_a_warned_partial_total(caplog: pytest.LogCaptureFixture) -> None:
    """Ignore invalid readings without exposing their values or treating them as zero."""
    meter = OceaMeter.from_api({"id": "pds-private", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [
        {"date": "2026-09-01", "valeur": "0,1"},
        {"date": "2026-09-02", "valeur": "private-value"},
        {"date": "private-date", "valeur": 10},
        {"date": "2026-09-03", "valeur": "0,2", "fuiteEstimee": "0,1"},
    ]})
    with caplog.at_level(logging.WARNING):
        reading = _build_meter_data(meter, response)
    assert reading.value == Decimal("0.3")
    assert reading.latest_date is None
    assert reading.estimated_leak is None
    assert "total may be incomplete" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    ("invalid_date", "expected_leak"),
    [("2026-09-01", Decimal("0.1")), ("2026-09-03", None)],
)
def test_invalid_reading_cannot_cause_stale_leak_metadata(
    invalid_date: str, expected_leak: Decimal | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep valid latest metadata only when the invalid reading is known to be older."""
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [
        {"date": "2026-09-02", "valeur": 1, "fuiteEstimee": "0,1"},
        {"date": invalid_date, "valeur": "invalid", "fuiteEstimee": "0"},
    ]})
    with caplog.at_level(logging.WARNING):
        reading = _build_meter_data(meter, response)
    assert reading.value == Decimal(1)
    assert reading.estimated_leak == expected_leak
    assert "total may be incomplete" in caplog.text
    if expected_leak is None:
        assert "metadata is unavailable" in caplog.text


def test_all_invalid_readings_are_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    """An entirely invalid series cannot become a zero consumption."""
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [{"valeur": 5}]})
    with caplog.at_level(logging.WARNING):
        reading = _build_meter_data(meter, response)
    assert reading.value is None
    assert reading.latest_date is None
    assert "total may be incomplete" in caplog.text
    assert _build_meter_data(meter, OceaConsumptionSeries.from_api({})).value == Decimal(0)


def test_latest_instant_respects_explicit_offsets() -> None:
    """Compare actual instants rather than the lexical order of timestamps."""
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [
        {"date": "2026-09-10T12:00:00+02:00", "valeur": 1, "fuiteEstimee": "0,1"},
        {"date": "2026-09-10T10:30:00Z", "valeur": 2, "fuiteEstimee": "0,2"},
    ]})
    reading = _build_meter_data(meter, response)
    assert reading.latest_date == datetime(2026, 9, 10, 10, 30, tzinfo=timezone.utc)
    assert reading.estimated_leak == Decimal("0.2")


def test_latest_instant_orders_both_folds_of_autumn_clock_change() -> None:
    """The second 02:15 occurs after the first 02:45 in Paris."""
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [
        {"date": "2024-10-27T02:15:00+01:00", "valeur": 1, "fuiteEstimee": "0,2"},
        {"date": "2024-10-27T02:45:00+02:00", "valeur": 2, "fuiteEstimee": "0,1"},
    ]})
    reading = _build_meter_data(meter, response)
    assert reading.latest_date == datetime(
        2024, 10, 27, 1, 15, tzinfo=timezone.utc
    )
    assert reading.estimated_leak == Decimal("0.2")


def test_mixed_day_and_instant_metadata_is_not_guessed(caplog: pytest.LogCaptureFixture) -> None:
    """An unspecific date and a timestamp on the same day have no definite ordering."""
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    response = OceaConsumptionSeries.from_api({"consommations": [
        {"date": "2026-09-10", "valeur": 1, "fuiteEstimee": 0.1},
        {"date": "2026-09-10T12:00:00", "valeur": 2, "fuiteEstimee": 0.2},
    ]})
    with caplog.at_level(logging.WARNING):
        reading = _build_meter_data(meter, response)
    assert reading.value == Decimal(3)
    assert reading.latest_date == date(2026, 9, 10)
    assert reading.estimated_leak is None
    assert "metadata is unavailable" in caplog.text


def test_sensor_values_remain_floats() -> None:
    """Keep Home Assistant's existing externally exposed numeric values."""
    coordinator = Mock()
    meter = OceaMeter.from_api({"id": "pds", "fluide": "EauFroide"}, None)
    coordinator.data = OceaData(
        totals={"cetc": Decimal("1.25")},
        meters={"pds": OceaMeterReading(
            meter=meter, value=Decimal("0.321"), latest_date=date(2026, 9, 15),
            estimated_leak=Decimal("0.015"),
        )},
    )
    description = next(description for description in SENSOR_TYPES if description.key == "fuite")
    sensor = OceaMeterSensor(coordinator, description, "local", meter)
    assert sensor.native_value == 0.015
    assert isinstance(sensor.native_value, float)


async def test_coordinator_isolates_malformed_meter_response(
    hass: HomeAssistant,
) -> None:
    """Test malformed meter data does not discard other meter readings or totals."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = [
        OceaDashboardData.from_api({"fluide": "EauFroide", "valeur": "1,14"}),
    ]
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-cold-1", "fluide": "EauFroide"}),
        OceaPdsData.from_api({"id": "pds-cold-2", "fluide": "EauFroide"}),
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.side_effect = [
        OceaConsumptionSeries.from_api({"consommations": [{"date": "2026-09-15", "valeur": 0.321}]}),
        OceaConsumptionSeries.from_api({"consommations": [{"date": "2026-09-15", "valeur": "invalid"}]}),
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == Decimal("1.14")
    assert data.meters["pds-cold-1"].value == Decimal("0.321")
    assert data.meters["pds-cold-2"].value is None


async def test_coordinator_discovers_meters_only_once(
    hass: HomeAssistant,
) -> None:
    """Test PDS metadata is cached while values continue to refresh."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-cold-1", "fluide": "EauFroide"}),
    ]
    client.get_appareils.return_value = [
        OceaDeviceData.from_api({"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"}),
    ]
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api({
        "consommations": [{"date": "2026-09-15", "valeur": 0.321}],
    })
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    client.get_pds.assert_called_once_with("local-123")
    client.get_appareils.assert_called_once_with({"pdsIds": ["pds-cold-1"]})
    assert client.get_pds_consumption.call_count == 2


@pytest.mark.parametrize("error", [OceaApiError("meter unavailable"), TimeoutError()])
async def test_coordinator_keeps_other_meters_when_one_fails(
    hass: HomeAssistant,
    error: Exception,
) -> None:
    """Test one PDS failure does not discard other meter data or totals."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = [
        OceaDashboardData.from_api({"fluide": "EauFroide", "valeur": "1,14"}),
    ]
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-cold-1", "fluide": "EauFroide"}),
        OceaPdsData.from_api({"id": "pds-cold-2", "fluide": "EauFroide"}),
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.side_effect = [
        OceaConsumptionSeries.from_api({"consommations": [{"date": "2026-09-15", "valeur": 0.321}]}),
        error,
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == Decimal("1.14")
    assert set(data.meters) == {"pds-cold-1", "pds-cold-2"}
    assert data.meters["pds-cold-2"].value is None


async def test_coordinator_does_not_log_meter_details(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test coordinator logs do not contain PDS or serial identifiers."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        OceaPdsData.from_api({"id": "pds-secret", "fluide": "EauFroide"}),
    ]
    client.get_appareils.return_value = [
        OceaDeviceData.from_api({"pdsId": "pds-secret", "numeroSerie": "serial-secret"}),
    ]
    client.get_pds_consumption.return_value = OceaConsumptionSeries.from_api({"consommations": []})
    coordinator = _coordinator(hass, client)

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_setup()
        await coordinator._async_update_data()

    assert "pds-secret" not in caplog.text
    assert "serial-secret" not in caplog.text


def test_meter_sensor_uses_pds_id_and_serial_for_identity() -> None:
    """Test individual meters get stable identities and readable names."""
    coordinator = Mock()
    description = next(
        description
        for description in SENSOR_TYPES
        if description.key == "fuite"
    )
    sensor = OceaMeterSensor(
        coordinator=coordinator,
        description=description,
        local_id="local-123",
        meter=OceaMeter.from_api(
            {
                "id": "pds-cold-1",
                "fluide": "EauFroide",
                "emplacement": "Cuisine",
            },
            {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
        ),
    )

    assert sensor.unique_id == (
        "ocea_smart_building_local-123_pds-cold-1_fuite"
    )
    assert sensor.has_entity_name is True
    assert sensor.entity_description.translation_key == "fuite"
    assert sensor.entity_description.name is UNDEFINED
    assert not hasattr(sensor, "_attr_name")
    assert sensor.device_info["identifiers"] == {
        ("ocea_smart_building", "local-123_pds-cold-1")
    }
    assert sensor.device_info["translation_key"] == "meter"
    assert sensor.device_info["translation_placeholders"] == {
        "meter_identity": "SERIAL-1 (PDS pds-cold-1)"
    }
    assert sensor.device_info["suggested_area"] == "Cuisine"


async def test_coordinator_setup_maps_auth_error_to_reauth(
    hass: HomeAssistant,
) -> None:
    """Test a rejected discovery requests reauthentication."""
    client = AsyncMock(spec=OceaApiClient)
    client.get_pds.side_effect = OceaAuthError("rejected")
    coordinator = _coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_setup()
