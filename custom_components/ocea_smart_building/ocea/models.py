"""Models of Ocea API data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .exceptions import OceaApiError


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise OceaApiError(f"Invalid meter field: {key}")
    return value


def _optional_number(data: Mapping[str, object], key: str) -> float | None:
    value = data.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise OceaApiError(f"Invalid meter field: {key}")
    return value


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
    def from_api(cls, data: Mapping[str, object]) -> OceaPds:
        """Build a PDS model from an Ocea response."""
        return cls(
            id=str(data.get("id", "")),
            fluid=str(data.get("fluide", "")),
            code=_optional_string(data, "code"),
            location=_optional_string(data, "emplacement"),
            reading_type=_optional_string(data, "typeReleve"),
            fce=_optional_number(data, "fce"),
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

    @classmethod
    def from_api(
        cls,
        pds_data: Mapping[str, object],
        device_data: Mapping[str, object] | None,
    ) -> OceaMeter:
        """Build a meter model from PDS and device responses."""
        device = device_data or {}
        return cls(
            pds=OceaPds.from_api(pds_data),
            device_id=_optional_string(device, "id"),
            serial_number=_optional_string(device, "numeroSerie"),
            installation_date=_optional_string(device, "datePose"),
            correction_factor=_optional_number(device, "fc"),
        )
