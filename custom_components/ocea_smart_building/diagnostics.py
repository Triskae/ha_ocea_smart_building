"""Diagnostics support for Ocea Smart Building."""

from __future__ import annotations

from typing import TypedDict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator

# Meter identifiers and location details can identify a dwelling.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_LOCAL_ID,
    "pds_id",
    "device_id",
    "serial_number",
    "code",
    "location",
}


class MeterDiagnostics(TypedDict):
    """Meter metadata included in diagnostics."""

    pds_id: str
    fluid: str
    code: str | None
    location: str | None
    reading_type: str | None
    fce: float | None
    device_id: str | None
    serial_number: str | None
    installation_date: str | None
    correction_factor: float | None


class ReadingDiagnostics(TypedDict):
    """Consumption reading included in diagnostics."""

    meter: MeterDiagnostics
    value: float | None
    latest_date: str | None
    unit: str | None
    estimated_leak: float | None
    consent: bool | None
    response_type: str | None


class CoordinatorDiagnostics(TypedDict):
    """Coordinator data included in diagnostics."""

    totals: dict[str, float]
    meters: list[ReadingDiagnostics]


class EntryDiagnostics(TypedDict):
    """Diagnostics payload for one configuration entry."""

    entry_data: dict[str, object]
    last_update_success: bool
    data: CoordinatorDiagnostics | None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry[object]
) -> dict[str, object]:
    """Return diagnostics for a config entry."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    diagnostic_data: CoordinatorDiagnostics | None = None
    if data is not None:
        diagnostic_data = {
            "totals": {
                key: value
                for key, value in data.totals.items()
                if key in {"eau_froide", "eau_chaude", "cetc"}
            },
            "meters": [
                {
                    "meter": {
                        "pds_id": reading.meter.identifier,
                        "fluid": reading.meter.pds.fluid,
                        "code": reading.meter.pds.code,
                        "location": reading.meter.pds.location,
                        "reading_type": reading.meter.pds.reading_type,
                        "fce": reading.meter.pds.fce,
                        "device_id": reading.meter.device_id,
                        "serial_number": reading.meter.serial_number,
                        "installation_date": reading.meter.installation_date,
                        "correction_factor": reading.meter.correction_factor,
                    },
                    "value": reading.value,
                    "latest_date": reading.latest_date,
                    "unit": reading.unit,
                    "estimated_leak": reading.estimated_leak,
                    "consent": reading.consent,
                    "response_type": reading.response_type,
                }
                for reading in data.meters.values()
            ],
        }

    return async_redact_data(
        EntryDiagnostics(
            entry_data={
                key: entry.data[key]
                for key in (CONF_EMAIL, CONF_PASSWORD, CONF_LOCAL_ID)
                if key in entry.data
            },
            last_update_success=coordinator.last_update_success,
            data=diagnostic_data,
        ),
        TO_REDACT,
    )
