"""Typed models for Ocea water-meter data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OceaPds:
    """Ocea point of measurement metadata."""

    id: str
    fluid: str
    code: str | None = None
    location: str | None = None
    reading_type: str | None = None
    fce: float | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> OceaPds:
        """Build a PDS model from an Ocea response."""
        return cls(
            id=str(data.get("id", "")),
            fluid=str(data.get("fluide", "")),
            code=data.get("code"),
            location=data.get("emplacement"),
            reading_type=data.get("typeReleve"),
            fce=data.get("fce"),
        )


@dataclass(frozen=True, slots=True)
class OceaMeter:
    """Ocea PDS enriched with its installed device metadata."""

    pds: OceaPds
    device_id: str | None = None
    serial_number: str | None = None
    installation_date: str | None = None
    correction_factor: float | None = None

    @property
    def identifier(self) -> str:
        """Return the stable Ocea identifier for this meter."""
        return self.pds.id

    @property
    def display_name(self) -> str:
        """Return a human-readable meter identity."""
        if self.serial_number:
            return f"{self.serial_number} (PDS {self.identifier})"
        return f"PDS {self.identifier}"

    @classmethod
    def from_api(
        cls,
        pds_data: dict[str, Any],
        device_data: dict[str, Any] | None,
    ) -> OceaMeter:
        """Build a meter model from PDS and device responses."""
        device = device_data or {}
        return cls(
            pds=OceaPds.from_api(pds_data),
            device_id=device.get("id"),
            serial_number=device.get("numeroSerie"),
            installation_date=device.get("datePose"),
            correction_factor=device.get("fc"),
        )


@dataclass(frozen=True, slots=True)
class OceaMeterReading:
    """Current-month consumption for one Ocea meter."""

    meter: OceaMeter
    value: float | None
    latest_date: str | None
    unit: str | None = None
    estimated_leak: float | None = None
    consent: bool | None = None
    response_type: str | None = None


@dataclass(frozen=True, slots=True)
class OceaData:
    """Coordinator payload: dwelling totals and per-meter readings."""

    totals: dict[str, float]
    meters: dict[str, OceaMeterReading]
