"""Models of Ocea API data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import cast

from .exceptions import OceaApiError


class OccupationStatus(StrEnum):
    """Indicative status derived from occupation dates."""

    CURRENT = "current"
    FUTURE = "future"
    ENDED = "ended"
    UNKNOWN = "unknown"


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.utcoffset() is not None else None


@dataclass(frozen=True, slots=True)
class OceaOccupation:
    """Untrusted occupation dates associated with a dwelling."""

    start: datetime | None
    end: datetime | None
    open_ended: bool

    @classmethod
    def from_api(cls, data: Mapping[str, object]) -> OceaOccupation:
        """Parse dates without assuming absent or malformed values are valid."""
        return cls(
            start=_instant(data.get("dateDebut")),
            end=_instant(data.get("dateFin")),
            open_ended="dateFin" in data and data["dateFin"] is None,
        )

    def status(self, now: datetime) -> OccupationStatus:
        """Return a conservative, indicative status at an instant in UTC."""
        if self.start is None or (self.end is None and not self.open_ended):
            return OccupationStatus.UNKNOWN
        if self.end is not None and (self.end <= self.start or self.end == now):
            return OccupationStatus.UNKNOWN
        if self.start == now:
            return OccupationStatus.UNKNOWN
        if self.start > now:
            return OccupationStatus.FUTURE
        if self.end is not None and self.end < now:
            return OccupationStatus.ENDED
        return OccupationStatus.CURRENT


@dataclass(frozen=True, slots=True)
class OceaDwelling:
    """A dwelling and its associated occupations."""

    local_id: str
    occupations: tuple[OceaOccupation, ...]

    def status(self, now: datetime) -> OccupationStatus:
        """Only report a status if all its occupations agree."""
        statuses = {occupation.status(now) for occupation in self.occupations}
        return statuses.pop() if len(statuses) == 1 else OccupationStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class OceaResident:
    """Resident display name and deduplicated dwellings."""

    name: str
    dwellings: tuple[OceaDwelling, ...]

    @classmethod
    def from_api(cls, data: Mapping[str, object]) -> OceaResident:
        """Validate resident and occupation shapes at the API boundary."""
        resident = data.get("resident", {})
        if not isinstance(resident, dict):
            raise OceaApiError("Invalid resident response")
        resident = cast(dict[str, object], resident)
        name = resident.get("prenom", "")
        if not isinstance(name, str):
            raise OceaApiError("Invalid resident name")
        raw_occupations = data.get("occupations", [])
        if not isinstance(raw_occupations, list):
            raise OceaApiError("Invalid occupations response")
        dwellings: dict[str, list[OceaOccupation]] = {}
        for raw in cast(list[object], raw_occupations):
            if not isinstance(raw, dict):
                raise OceaApiError("Invalid occupation response")
            occupation = cast(dict[str, object], raw)
            local_id = occupation.get("logementId")
            if not isinstance(local_id, str) or not local_id.strip():
                raise OceaApiError("Invalid dwelling identifier")
            dwellings.setdefault(local_id, []).append(OceaOccupation.from_api(occupation))
        return cls(
            name=name,
            dwellings=tuple(
                OceaDwelling(local_id, tuple(occupations))
                for local_id, occupations in dwellings.items()
            ),
        )


def dwelling_address(data: Mapping[str, object]) -> str | None:
    """Extract the available address components from dwelling details."""
    local = data.get("local")
    if not isinstance(local, dict):
        raise OceaApiError("Invalid dwelling details")
    address = local.get("adresse")
    if address is None:
        return None
    if not isinstance(address, dict):
        raise OceaApiError("Invalid dwelling address")
    parts: list[str] = []
    for key in ("numeroRue", "complement", "codePostal", "ville", "pays"):
        value: object = address.get(key)
        if value is not None and not isinstance(value, (str, int)):
            raise OceaApiError("Invalid dwelling address field")
        if value is not None and str(value).strip():
            parts.append(str(value).strip())
    return ", ".join(parts) or None


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
