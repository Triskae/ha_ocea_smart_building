"""Tests for typed Ocea meter models."""

from __future__ import annotations

from custom_components.ocea_smart_building.models import (
    OceaMeter,
    OceaMeterReading,
    OceaPds,
)


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
    assert meter.display_name == "SERIAL-1 (PDS pds-cold-1)"


def test_meter_display_name_falls_back_to_pds_id() -> None:
    """Test meter naming remains useful without a serial number."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    assert meter.display_name == "PDS pds-cold-1"


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
        value=0.321,
        latest_date="2026-09-15",
        unit="m3",
        estimated_leak=0.0,
        consent=True,
        response_type="ConsommationEauResponse",
    )

    assert reading.unit == "m3"
    assert reading.estimated_leak == 0.0
    assert reading.consent is True
    assert reading.response_type == "ConsommationEauResponse"
