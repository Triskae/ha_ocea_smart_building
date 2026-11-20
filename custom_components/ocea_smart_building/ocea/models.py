"""Models of Ocea API data."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, NotRequired, TypedDict, TypeGuard, cast

from .const import OCEA_TIME_ZONE
from .exceptions import OceaApiError

_LOGGER = logging.getLogger(__name__)


class OceaDevicesRequest(TypedDict):
    """Request for devices associated with measurement points."""

    pdsIds: list[str]


class OceaConsumptionRequest(TypedDict):
    """Date range and granularity for a consumption request."""

    debut: str
    fin: str
    granularity: str


class OceaDashboardConsumption(TypedDict, total=False):
    """Known fields of a dashboard consumption."""

    fluide: str
    valeur: object
    unite: str | None


class OceaPdsResponse(TypedDict, total=False):
    """Point-of-measurement fields used for discovery."""

    id: str | int | None
    fluide: str | None


class OceaDeviceResponse(TypedDict, total=False):
    """Device field used to match a measurement point."""

    pdsId: str | int | None


class OceaConsumptionBase(TypedDict, total=False):
    """Fields shared by the portal's dated consumption variants."""

    date: object
    valeur: object
    consentement: bool | None


class OceaGenericConsumption(OceaConsumptionBase):
    """Generic consumption identified by the portal."""

    type: Literal["ConsommationResponse"]
    isEstimated: NotRequired[bool | None]


class OceaWaterConsumption(OceaConsumptionBase):
    """Water consumption and its optional estimated leak."""

    type: Literal["ConsommationEauResponse"]
    isEstimated: NotRequired[bool | None]
    fuiteEstimee: NotRequired[str | int | float | None]


class OceaRfcConsumption(OceaConsumptionBase):
    """Heating allocator consumption identified by the portal."""

    type: Literal["ConsommationRfcResponse"]
    isEstimated: NotRequired[bool | None]
    valeurCoefficientee: NotRequired[str | int | float | None]
    emplacement: NotRequired[str | None]


class OceaUnclassifiedConsumption(OceaConsumptionBase, total=False):
    """Compatible fallback when a response cannot be classified safely."""

    type: str | None
    isEstimated: object
    fuiteEstimee: str | int | float | None


type OceaConsumptionItem = (
    OceaGenericConsumption
    | OceaWaterConsumption
    | OceaRfcConsumption
    | OceaUnclassifiedConsumption
)


def is_water_consumption(item: OceaConsumptionItem) -> TypeGuard[OceaWaterConsumption]:
    """Identify a water item whose optional estimate flag has a known type."""
    estimated = item.get("isEstimated")
    return item.get("type") == "ConsommationEauResponse" and (
        estimated is None or isinstance(estimated, bool)
    )


def is_rfc_consumption(item: OceaConsumptionItem) -> TypeGuard[OceaRfcConsumption]:
    """Identify an RFC item only when its optional fields have known types."""
    estimated = item.get("isEstimated")
    coefficient = item.get("valeurCoefficientee")
    location = item.get("emplacement")
    return (
        item.get("type") == "ConsommationRfcResponse"
        and (estimated is None or isinstance(estimated, bool))
        and (coefficient is None or (
            not isinstance(coefficient, bool) and isinstance(coefficient, (str, int, float))
        ))
        and (location is None or isinstance(location, str))
    )


def is_generic_consumption(item: OceaConsumptionItem) -> TypeGuard[OceaGenericConsumption]:
    """Identify a generic item whose optional estimate flag has a known type."""
    estimated = item.get("isEstimated")
    return item.get("type") == "ConsommationResponse" and (
        estimated is None or isinstance(estimated, bool)
    )


def has_leak_estimate(
    item: OceaConsumptionItem,
) -> TypeGuard[OceaWaterConsumption | OceaUnclassifiedConsumption]:
    """Identify validated leak fields, including legacy unclassified items."""
    return "fuiteEstimee" in item


class OceaConsumptionResponse(TypedDict, total=False):
    """Known fields of a measurement point's consumption response."""

    unite: str | None
    consommations: list[OceaConsumptionItem]


@dataclass(frozen=True, slots=True)
class OceaDashboardData:
    """Validated dashboard consumption."""

    fluid: str
    value: Decimal | None
    unit: str | None
    raw: Mapping[str, object] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: OceaDashboardConsumption) -> OceaDashboardData:
        """Convert a validated dashboard item."""
        return cls(data.get("fluide", ""), _decimal(data.get("valeur")), data.get("unite"), data)


@dataclass(frozen=True, slots=True)
class OceaPdsData:
    """A discovered measurement point before filtering unsupported fluids."""

    id: str | int | None
    fluid: str | None
    raw: Mapping[str, object] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: OceaPdsResponse) -> OceaPdsData:
        """Convert a validated point of measurement."""
        return cls(data.get("id"), data.get("fluide"), data)

    def as_meter(self, device: OceaDeviceData | None) -> OceaMeter:
        """Validate selected meter metadata and attach the matching device."""
        return OceaMeter.from_api(self.raw, device.raw if device is not None else None)


@dataclass(frozen=True, slots=True)
class OceaDeviceData:
    """Device response matched to a measurement point."""

    pds_id: str | int | None
    raw: Mapping[str, object] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: OceaDeviceResponse) -> OceaDeviceData:
        """Convert a validated device response."""
        return cls(data.get("pdsId"), data)


@dataclass(frozen=True, slots=True)
class OceaConsumptionData:
    """Common validated data for one dated consumption."""

    date: date | datetime | None
    value: Decimal | None
    consent: bool | None
    response_type: str | None
    raw: Mapping[str, object] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OceaGenericConsumptionData(OceaConsumptionData):
    """A generic consumption item."""

    is_estimated: bool | None


@dataclass(frozen=True, slots=True)
class OceaWaterConsumptionData(OceaConsumptionData):
    """A water consumption item."""

    is_estimated: bool | None
    estimated_leak: Decimal | None


@dataclass(frozen=True, slots=True)
class OceaRfcConsumptionData(OceaConsumptionData):
    """A heating allocator consumption item."""

    is_estimated: bool | None
    corrected_value: Decimal | None
    location: str | None


@dataclass(frozen=True, slots=True)
class OceaUnclassifiedConsumptionData(OceaConsumptionData):
    """An item with an unknown or insufficiently validated discriminator."""

    is_estimated: object
    estimated_leak: Decimal | None


type OceaConsumptionDataItem = (
    OceaGenericConsumptionData
    | OceaWaterConsumptionData
    | OceaRfcConsumptionData
    | OceaUnclassifiedConsumptionData
)


def _decimal(value: object) -> Decimal | None:
    """Convert an API number without treating missing or invalid values as zero."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _optional_decimal(value: object) -> Decimal | None:
    """Convert optional numeric metadata and report invalid supplied values."""
    result = _decimal(value)
    if value is not None and result is None:
        _LOGGER.warning("Invalid optional consumption value")
    return result


def _reading_date(value: object) -> date | datetime | None:
    """Parse a day or an instant, interpreting naive timestamps in Paris."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.utcoffset() is not None:
        return parsed
    candidates = [
        parsed.replace(tzinfo=OCEA_TIME_ZONE, fold=fold)
        for fold in (0, 1)
    ]
    valid = [
        candidate
        for candidate in candidates
        if candidate.astimezone(timezone.utc).astimezone(OCEA_TIME_ZONE).replace(tzinfo=None) == parsed
    ]
    if not valid or (len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset()):
        return None
    return valid[0]


def _consumption_data(item: OceaConsumptionItem) -> OceaConsumptionDataItem:
    """Convert a validated item without assuming unknown variants are invalid."""
    common = (
        _reading_date(item.get("date")),
        _decimal(item.get("valeur")),
        item.get("consentement"),
        item.get("type"),
        item,
    )
    if is_water_consumption(item):
        return OceaWaterConsumptionData(
            *common, is_estimated=item.get("isEstimated"),
            estimated_leak=_optional_decimal(item.get("fuiteEstimee")),
        )
    if not has_leak_estimate(item):
        if is_rfc_consumption(item):
            return OceaRfcConsumptionData(
                *common,
                is_estimated=item.get("isEstimated"),
                corrected_value=_optional_decimal(item.get("valeurCoefficientee")),
                location=item.get("emplacement"),
            )
        if is_generic_consumption(item):
            return OceaGenericConsumptionData(*common, is_estimated=item.get("isEstimated"))
    return OceaUnclassifiedConsumptionData(
        *common,
        is_estimated=item.get("isEstimated"),
        estimated_leak=_optional_decimal(item.get("fuiteEstimee")) if has_leak_estimate(item) else None,
    )


@dataclass(frozen=True, slots=True)
class OceaConsumptionSeries:
    """A validated meter or dwelling consumption response."""

    unit: str | None
    items: tuple[OceaConsumptionDataItem, ...]
    raw: Mapping[str, object] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: OceaConsumptionResponse) -> OceaConsumptionSeries:
        """Convert validated consumption items into typed variants."""
        return cls(
            unit=data.get("unite"),
            items=tuple(_consumption_data(item) for item in data.get("consommations", [])),
            raw=data,
        )


def _validate_fields(
    data: Mapping[str, object], fields: Mapping[str, tuple[type, ...]]
) -> None:
    """Validate present fields without rejecting unknown fields."""
    for key, allowed in fields.items():
        if key in data:
            value = data[key]
            if not isinstance(value, allowed) or (
                isinstance(value, bool) and bool not in allowed
            ):
                raise OceaApiError(f"Invalid API field: {key}")


def is_dashboard_consumption(data: dict[str, object]) -> TypeGuard[OceaDashboardConsumption]:
    """Validate fields used from a dashboard response."""
    _validate_fields(data, {"fluide": (str,), "unite": (str, type(None))})
    return True


def is_pds_response(data: dict[str, object]) -> TypeGuard[OceaPdsResponse]:
    """Validate fields used from a measurement point response."""
    _validate_fields(
        data,
        {
            "id": (str, int, type(None)),
            "fluide": (str, type(None)),
        },
    )
    return True


def is_device_response(data: dict[str, object]) -> TypeGuard[OceaDeviceResponse]:
    """Validate fields used from a device response."""
    _validate_fields(
        data,
        {
            "pdsId": (str, int, type(None)),
        },
    )
    return True


def is_consumption_response(data: dict[str, object]) -> TypeGuard[OceaConsumptionResponse]:
    """Validate consumption fields used by the coordinator."""
    _validate_fields(data, {"unite": (str, type(None))})
    if "consommations" in data:
        items = data["consommations"]
        if not isinstance(items, list):
            raise OceaApiError("Invalid meter consumption list")
        for item in items:
            if not isinstance(item, dict):
                raise OceaApiError("Invalid meter consumption item")
            _validate_fields(
                item,
                {
                    "fuiteEstimee": (str, int, float, type(None)),
                    "consentement": (bool, type(None)),
                    "type": (str, type(None)),
                },
            )
    return True


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


@dataclass(frozen=True, slots=True)
class OceaAddress:
    """Structured address returned by the dwelling endpoint."""

    street_number: str | None = None
    complement: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, object]) -> OceaAddress:
        """Validate and retain the available address components."""
        def field(key: str) -> str | None:
            value = data.get(key)
            if value is not None and not isinstance(value, (str, int)):
                raise OceaApiError("Invalid dwelling address field")
            return str(value) if value is not None else None

        return cls(
            street_number=field("numeroRue"),
            complement=field("complement"),
            postal_code=field("codePostal"),
            city=field("ville"),
            country=field("pays"),
        )


@dataclass(frozen=True, slots=True)
class OceaDwellingDetails:
    """Validated details used to distinguish an accessible dwelling."""

    address: OceaAddress | None

    @classmethod
    def from_api(cls, data: Mapping[str, object]) -> OceaDwellingDetails:
        """Parse the available address from dwelling details."""
        local = data.get("local")
        if not isinstance(local, dict):
            raise OceaApiError("Invalid dwelling details")
        address = local.get("adresse")
        if address is None:
            return cls(address=None)
        if not isinstance(address, dict):
            raise OceaApiError("Invalid dwelling address")
        return cls(address=OceaAddress.from_api(cast(dict[str, object], address)))


@dataclass(frozen=True, slots=True)
class OceaDwellingFluids:
    """Validated fluids advertised by the dwelling endpoint."""

    fluids: tuple[str, ...]

    @classmethod
    def from_api(cls, data: Mapping[str, object]) -> OceaDwellingFluids:
        """Parse the advertised fluids without requiring address details."""
        fluids = data.get("fluidesRestitues")
        if not isinstance(fluids, list):
            raise OceaApiError("Invalid dwelling fluids")
        result: list[str] = []
        for item in fluids:
            if not isinstance(item, dict) or not isinstance(item.get("fluide"), str):
                raise OceaApiError("Invalid dwelling fluid")
            result.append(item["fluide"])
        return cls(fluids=tuple(result))


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
