"""Tests for typed Ocea meter models."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from custom_components.ocea_smart_building.coordinator import OceaMeterReading
from custom_components.ocea_smart_building.entity import meter_device_info
from custom_components.ocea_smart_building.ocea import OceaApiError, OceaMeter, OceaPds
from custom_components.ocea_smart_building.ocea.models import (
    OccupationStatus,
    OceaAddress,
    OceaConsumptionSeries,
    OceaDashboardData,
    OceaDwellingDetails,
    OceaDwellingFluids,
    OceaOccupation,
    OceaResident,
    OceaRfcConsumptionData,
    OceaWaterConsumptionData,
    is_generic_consumption,
    is_rfc_consumption,
    is_water_consumption,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12,340", Decimal("12.340")),
        (0.25, Decimal("0.25")),
        (5, Decimal(5)),
        (None, None),
        (True, None),
        ("not-a-number", None),
        ("Infinity", None),
        ("NaN", None),
    ],
)
def test_consumption_values_are_finite_decimals(raw, expected) -> None:
    """Convert supported JSON numbers without inventing missing values."""
    data = OceaConsumptionSeries.from_api({"consommations": [{"valeur": raw}]})
    assert data.items[0].value == expected
    assert data.raw["consommations"] == [{"valeur": raw}]
    assert OceaDashboardData.from_api({"valeur": raw}).value == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02", date(2026, 1, 2)),
        ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=ZoneInfo("Europe/Paris"))),
        ("2026-07-02T03:04:05", datetime(2026, 7, 2, 3, 4, 5, tzinfo=ZoneInfo("Europe/Paris"))),
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        ("2026-01-02T03:04:05+02:00",
         datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))),
        ("2026-03-29T02:30:00", None),
        ("2026-10-25T02:30:00", None),
        ("invalid", None),
        (None, None),
        (False, None),
    ],
)
def test_consumption_dates_preserve_day_or_instant(raw, expected) -> None:
    """Preserve explicit offsets and reject ambiguous or nonexistent Paris times."""
    data = OceaConsumptionSeries.from_api({"consommations": [{"date": raw, "valeur": 1}]})
    assert data.items[0].date == expected
    assert type(data.items[0].date) is type(expected)
    if isinstance(expected, datetime) and isinstance(data.items[0].date, datetime):
        assert data.items[0].date.utcoffset() == expected.utcoffset()


def test_optional_consumption_numbers_are_typed_and_invalid_values_are_reported(caplog) -> None:
    """Keep corrected values precise and do not expose invalid metadata in logs."""
    data = {"consommations": [
        {"type": "ConsommationRfcResponse", "valeur": 1, "valeurCoefficientee": "1,50"},
        {"type": "ConsommationEauResponse", "valeur": 2, "fuiteEstimee": "private-value"},
    ]}
    series = OceaConsumptionSeries.from_api(data)
    corrected, water = series.items
    assert isinstance(corrected, OceaRfcConsumptionData)
    assert corrected.corrected_value == Decimal("1.50")
    assert isinstance(water, OceaWaterConsumptionData)
    assert water.estimated_leak is None
    assert "Invalid optional consumption value" in caplog.text
    assert "private-value" not in caplog.text


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"type": "ConsommationResponse", "isEstimated": True}, (True, False, False)),
        ({"type": "ConsommationEauResponse", "fuiteEstimee": None}, (False, True, False)),
        (
            {"type": "ConsommationRfcResponse", "valeurCoefficientee": "1,5",
             "emplacement": "synthetic"},
            (False, False, True),
        ),
        ({"type": "ConsommationRfcResponse", "valeurCoefficientee": None}, (False, False, True)),
        ({"type": "ConsommationRfcResponse", "valeurCoefficientee": True}, (False, False, False)),
        ({"type": "ConsommationRfcResponse", "emplacement": 42}, (False, False, False)),
        ({"type": "ConsommationEauResponse", "isEstimated": "unknown"}, (False, False, False)),
        ({"type": "FutureResponse"}, (False, False, False)),
        ({}, (False, False, False)),
        ({"type": None}, (False, False, False)),
    ],
)
def test_consumption_variant_guards(item, expected) -> None:
    """Only refine a variant when its discriminator and typed fields are valid."""
    assert (
        is_generic_consumption(item),
        is_water_consumption(item),
        is_rfc_consumption(item),
    ) == expected


def test_pds_model_maps_ocea_response() -> None:
    """Test PDS fields are normalized from the Ocea response."""
    pds = OceaPds.from_api(
        {
            "id": "pds-cold-1",
            "code": "code-1",
            "fluide": "EauFroide",
            "emplacement": "Cuisine",
            "typeReleve": "Tele",
            "fce": 1.0,
        }
    )

    assert pds == OceaPds(
        id="pds-cold-1",
        fluid="EauFroide",
        code="code-1",
        location="Cuisine",
        reading_type="Tele",
        fce=1.0,
    )


def test_meter_model_links_pds_and_device() -> None:
    """Test a PDS is enriched with its Ocea device metadata."""
    meter = OceaMeter.from_api(
        {
            "id": "pds-cold-1",
            "fluide": "EauFroide",
        },
        {
            "id": "device-1",
            "pdsId": "pds-cold-1",
            "numeroSerie": "SERIAL-1",
            "datePose": "2023-07-07T00:00:00Z",
        },
    )

    assert meter.pds.id == "pds-cold-1"
    assert meter.serial_number == "SERIAL-1"
    assert meter.device_id == "device-1"
    assert meter.identifier == "pds-cold-1"
    assert meter_device_info("local-123", meter)["translation_placeholders"] == {
        "meter_identity": "SERIAL-1 (PDS pds-cold-1)"
    }


def test_meter_display_name_falls_back_to_pds_id() -> None:
    """Test meter naming remains useful without a serial number."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    assert meter_device_info("local-123", meter)["translation_placeholders"] == {
        "meter_identity": "PDS pds-cold-1"
    }


def test_meter_reading_can_be_unavailable() -> None:
    """Test an unavailable reading still identifies its meter."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = OceaMeterReading(meter=meter, value=None, latest_date=None)

    assert reading.meter is meter
    assert reading.value is None
    assert reading.latest_date is None


def test_meter_keeps_ocea_device_factor() -> None:
    """Test the device correction factor is retained when provided."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"pdsId": "pds-cold-1", "fc": 1.25},
    )

    assert meter.correction_factor == 1.25


def test_meter_reading_keeps_consumption_metadata() -> None:
    """Test consumption metadata is represented by the typed reading."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = OceaMeterReading(
        meter=meter,
        value=Decimal("0.321"),
        latest_date=date(2026, 9, 15),
        unit="m3",
        estimated_leak=Decimal(0),
        consent=True,
        response_type="ConsommationEauResponse",
    )

    assert reading.unit == "m3"
    assert reading.estimated_leak == 0.0
    assert reading.consent is True
    assert reading.response_type == "ConsommationEauResponse"


@pytest.mark.parametrize(
    ("field", "value"),
    [("numeroSerie", 3), ("id", []), ("datePose", False), ("fc", "invalid")],
)
def test_invalid_device_fields_are_explicit_errors(field: str, value: object) -> None:
    """Reject invalid response types without including field values in errors."""
    with pytest.raises(OceaApiError, match="Invalid meter field"):
        OceaMeter.from_api({"id": "pds-1"}, {field: value})


def test_invalid_pds_factor_is_an_explicit_error() -> None:
    with pytest.raises(OceaApiError, match="Invalid meter field: fce"):
        OceaPds.from_api({"id": "pds-1", "fce": True})


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2025-01-01T00:00:00Z", None, OccupationStatus.CURRENT),
        ("2027-01-01T01:00:00+01:00", None, OccupationStatus.FUTURE),
        ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", OccupationStatus.ENDED),
        ("2024-01-01T00:00:00Z", "2027-01-01T00:00:00Z", OccupationStatus.CURRENT),
        ("2026-01-01T00:00:00Z", None, OccupationStatus.UNKNOWN),
        ("2024-01-01T00:00:00Z", "2026-01-01T00:00:00Z", OccupationStatus.UNKNOWN),
        ("2027-01-01T00:00:00Z", "2025-01-01T00:00:00Z", OccupationStatus.UNKNOWN),
        ("2024-01-01", None, OccupationStatus.UNKNOWN),
        ("not-a-date", None, OccupationStatus.UNKNOWN),
        ("2024-01-01T00:00:00Z", "bad", OccupationStatus.UNKNOWN),
        (None, None, OccupationStatus.UNKNOWN),
    ],
)
def test_occupation_status(start: object, end: object, expected: OccupationStatus) -> None:
    occupation = OceaOccupation.from_api({"dateDebut": start, "dateFin": end})
    assert occupation.status(datetime(2026, 1, 1, tzinfo=timezone.utc)) == expected


def test_occupation_missing_end_is_unknown() -> None:
    occupation = OceaOccupation.from_api({"dateDebut": "2024-01-01T00:00:00Z"})
    assert occupation.status(datetime(2026, 1, 1, tzinfo=timezone.utc)) is OccupationStatus.UNKNOWN


def test_resident_deduplicates_and_does_not_infer_conflicting_statuses() -> None:
    resident = OceaResident.from_api({
        "resident": {"prenom": "Synthetic"},
        "occupations": [
            {"logementId": "one", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None},
            {"logementId": "one", "dateDebut": "2027-01-01T00:00:00Z", "dateFin": None},
        ],
    })
    assert len(resident.dwellings) == 1
    assert resident.dwellings[0].status(datetime(2026, 1, 1, tzinfo=timezone.utc)) is OccupationStatus.UNKNOWN


@pytest.mark.parametrize(
    "data",
    [
        {"resident": []},
        {"resident": {"prenom": 12}},
        {"occupations": {}},
        {"occupations": [None]},
        {"occupations": [{"logementId": ""}]},
        {"occupations": [{"logementId": 2}]},
    ],
)
def test_invalid_resident_fields_raise(data: dict[str, object]) -> None:
    with pytest.raises(OceaApiError):
        OceaResident.from_api(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"local": {}}, None),
        ({"local": {}, "fluidesRestitues": None}, None),
        ({"local": {"adresse": {}}}, OceaAddress()),
        (
            {"local": {"adresse": {
                "numeroRue": 7,
                "complement": " Unit A ",
                "ville": "Example",
                "codePostal": "12345",
                "pays": "Country",
            }}},
            OceaAddress(
                street_number="7",
                complement=" Unit A ",
                postal_code="12345",
                city="Example",
                country="Country",
            ),
        ),
    ],
)
def test_dwelling_address(data: dict[str, object], expected: OceaAddress | None) -> None:
    assert OceaDwellingDetails.from_api(data) == OceaDwellingDetails(address=expected)


@pytest.mark.parametrize(
    "data", [{}, {"local": {"adresse": []}}, {"local": {"adresse": {"ville": []}}}]
)
def test_invalid_dwelling_details_raise(data: dict[str, object]) -> None:
    with pytest.raises(OceaApiError):
        OceaDwellingDetails.from_api(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"fluidesRestitues": []}, ()),
        (
            {"local": None, "fluidesRestitues": [
                {"fluide": "EauFroide"}, {"fluide": "Cetc"}, {"fluide": "Other"}
            ]},
            ("EauFroide", "Cetc", "Other"),
        ),
    ],
)
def test_dwelling_fluids(data: dict[str, object], expected: tuple[str, ...]) -> None:
    """Map all advertised fluids independently of the address."""
    assert OceaDwellingFluids.from_api(data).fluids == expected


@pytest.mark.parametrize(
    "data",
    [{}, {"fluidesRestitues": None}, {"fluidesRestitues": [None]},
     {"fluidesRestitues": [{}]}, {"fluidesRestitues": [{"fluide": None}]}],
)
def test_invalid_dwelling_fluids_raise(data: dict[str, object]) -> None:
    """Reject malformed discovery data instead of treating it as an empty list."""
    with pytest.raises(OceaApiError, match="Invalid dwelling fluid"):
        OceaDwellingFluids.from_api(data)
