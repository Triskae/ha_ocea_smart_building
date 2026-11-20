"""Asynchronous client for the Ocea resident API."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Mapping
from hashlib import blake2b
from itertools import islice
from json import JSONDecodeError
from typing import TypeGuard, TypeVar, cast
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout

from .auth import OceaAuthentication
from .const import API_BASE, B2C_REDIRECT_URI
from .exceptions import OceaApiError
from .models import (
    OceaConsumptionRequest,
    OceaConsumptionSeries,
    OceaDashboardData,
    OceaDeviceData,
    OceaDevicesRequest,
    OceaDwellingDetails,
    OceaDwellingFluids,
    OceaPdsData,
    OceaResident,
    is_consumption_response,
    is_dashboard_consumption,
    is_device_response,
    is_pds_response,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=30)
_ResponseT = TypeVar("_ResponseT", bound=Mapping[str, object])
_KNOWN_FIELDS = frozenset({
    "resident", "occupations", "local", "adresse", "id", "logementId",
    "dateDebut", "dateFin", "prenom", "numeroRue", "complement", "codePostal",
    "ville", "pays", "fluide", "valeur", "unite", "code", "emplacement",
    "typeReleve", "fce", "pdsId", "numeroSerie", "datePose", "fc",
    "consommations", "date", "fuiteEstimee", "consentement", "type",
    "isEstimated", "valeurCoefficientee",
})
_SAFE_VALUES = {
    "fluide": frozenset({"EauFroide", "EauChaude", "Cetc", "Cetf", "Rfc"}),
    "type": frozenset({
        "ConsommationResponse", "ConsommationEauResponse", "ConsommationRfcResponse"
    }),
    "unite": frozenset({"m3", "kWh"}),
}


class OceaApiClient:
    """Client for the resident API using an externally owned session."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        user_agent: str,
        session: ClientSession,
    ) -> None:
        """Initialize the API client without taking ownership of the session."""
        self._user_agent = user_agent
        self._session = session
        self._auth = OceaAuthentication(session, email, password, user_agent)
        self._diagnostic_key = secrets.token_bytes(16)

    async def _api_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        diagnostic: str | None = None,
    ) -> object:
        """Make an authenticated API request, refreshing once on 401."""
        if not self._auth.access_token:
            await self._auth.authenticate()
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._auth.access_token}",
                "Accept": "application/json",
                "Origin": B2C_REDIRECT_URI,
                "Referer": f"{B2C_REDIRECT_URI}/",
                "User-Agent": self._user_agent,
            }
            if method == "POST":
                headers["Content-Type"] = "application/json"
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            ) as response:
                status = response.status
                if status == 200:
                    try:
                        result: object = await response.json(content_type=None)
                    except (JSONDecodeError, UnicodeDecodeError) as err:
                        raise OceaApiError("Invalid API JSON response") from err
                    if diagnostic is not None and _LOGGER.isEnabledFor(logging.DEBUG):
                        self._log_response(diagnostic, result)
                    return result
            if status == 401 and attempt == 0:
                _LOGGER.debug("Token expired, refreshing")
                await self._auth.refresh_access_token()
                continue
            raise OceaApiError(f"API error: HTTP {status}", status=status)
        raise OceaApiError("API authentication retry exhausted")

    async def get_resident(self) -> OceaResident:
        """Get and validate resident information and occupations."""
        result = await self._api_request("GET", "/api/v1/resident", diagnostic="resident")
        return OceaResident.from_api(_object_response(result))

    async def get_consumptions(self, local_id: str) -> list[OceaDashboardData]:
        """Get dwelling consumption data."""
        result = await self._api_request(
            "GET", f"/api/v1/local/{quote(local_id, safe='')}/dashboard/consos",
            diagnostic="dashboard",
        )
        return [
            OceaDashboardData.from_api(_validated_response(item, is_dashboard_consumption))
            for item in _list_response(result)
        ]

    async def check_dwelling_access(self, local_id: str) -> None:
        """Check that the account can access consumption data for a dwelling."""
        path_id = quote(local_id, safe="")
        _list_response(
            await self._api_request(
                "GET", f"/api/v1/local/{path_id}/dashboard/consos", diagnostic="dashboard"
            )
        )

    async def get_dwelling_details(self, local_id: str) -> OceaDwellingDetails:
        """Get and validate the details of an accessible dwelling."""
        path_id = quote(local_id, safe="")
        result = await self._api_request("GET", f"/api/v1/local/{path_id}", diagnostic="dwelling")
        return OceaDwellingDetails.from_api(_object_response(result))

    async def get_dwelling_fluids(self, local_id: str) -> list[str]:
        """Get the fluids advertised for a dwelling."""
        result = _object_response(
            await self._api_request(
                "GET", f"/api/v1/local/{quote(local_id, safe='')}", diagnostic="dwelling"
            )
        )
        return list(OceaDwellingFluids.from_api(result).fluids)

    async def get_pds(self, local_id: str) -> list[OceaPdsData]:
        """Get a dwelling's points of measurement."""
        result = await self._api_request(
            "GET", f"/api/v1/local/{quote(local_id, safe='')}/pds", diagnostic="pds"
        )
        return [
            OceaPdsData.from_api(_validated_response(item, is_pds_response))
            for item in _list_response(result)
        ]

    async def get_appareils(self, payload: OceaDevicesRequest) -> list[OceaDeviceData]:
        """Get the devices associated with the given points of measurement."""
        result = await self._api_request(
            "POST", "/api/v1/local/appareils", payload, diagnostic="devices"
        )
        return [
            OceaDeviceData.from_api(_validated_response(item, is_device_response))
            for item in _list_response(result)
        ]

    async def get_pds_consumption(
        self, local_id: str, pds_id: str, payload: OceaConsumptionRequest
    ) -> OceaConsumptionSeries:
        """Get consumption data for one point of measurement."""
        result = _object_response(
            await self._api_request(
                "POST",
                f"/api/v1/local/{quote(local_id, safe='')}/pds/{quote(pds_id, safe='')}/conso",
                payload,
                diagnostic="consumption",
            )
        )
        return OceaConsumptionSeries.from_api(_validated_response(result, is_consumption_response))

    async def get_dwelling_consumption(
        self, local_id: str, fluid: str, payload: OceaConsumptionRequest
    ) -> OceaConsumptionSeries:
        """Get consumption data for a dwelling and fluid."""
        result = _object_response(
            await self._api_request(
                "POST",
                f"/api/v1/local/{quote(local_id, safe='')}/conso/{quote(fluid, safe='')}",
                payload,
                diagnostic="consumption",
            )
        )
        return OceaConsumptionSeries.from_api(_validated_response(result, is_consumption_response))

    async def validate_credentials(self) -> None:
        """Validate credentials through B2C without requesting resident data."""
        await self._auth.authenticate()

    def _log_response(self, endpoint: str, response: object) -> None:
        """Log bounded JSON shape without exposing keys or values from the account."""
        pending: list[tuple[str, object, int, str]] = [("$", response, 0, "")]
        details: list[str] = []
        while pending and len(details) < 40:
            path, value, depth, field = pending.pop()
            category = _format_category(field, value)
            details.append(f"{path}:{type(value).__name__}{category}")
            if depth >= 4:
                continue
            if isinstance(value, dict):
                for key, item in islice(value.items(), 40):
                    if not isinstance(key, str):
                        continue
                    if key in _KNOWN_FIELDS:
                        pending.append((f"{path}.{key}", item, depth + 1, key))
                    else:
                        digest = blake2b(
                            key[:256].encode("utf-8", errors="surrogatepass"),
                            key=self._diagnostic_key,
                            digest_size=8,
                        ).hexdigest()
                        details.append(f"{path}.unknown#{digest}:{type(item).__name__}")
                        if len(details) >= 40:
                            break
            elif isinstance(value, list):
                details.append(f"{path}.length:{len(value)}")
                pending.extend((f"{path}[]", item, depth + 1, "") for item in value[:3])
        _LOGGER.debug("Ocea API %s shape: %s", endpoint, ", ".join(details[:40]))


def _format_category(field: str, value: object) -> str:
    """Classify a few known formats without including untrusted values."""
    if not isinstance(value, str) or len(value) > 40:
        return ""
    if value in _SAFE_VALUES.get(field, ()):
        return f"={value}"
    if field in {"date", "dateDebut", "dateFin", "datePose"}:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "(date)"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.+-]+Z?", value):
            return "(datetime)"
        return "(other-format)"
    if field in {"valeur", "fuiteEstimee", "valeurCoefficientee"}:
        if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value):
            return "(decimal-comma)" if "," in value else "(decimal-dot-or-integer)"
        return "(other-format)"
    return ""


def _object_response(response: object) -> dict[str, object]:
    """Validate the outer shape of an API object response."""
    if not isinstance(response, dict) or not all(isinstance(key, str) for key in response):
        raise OceaApiError("Invalid API object response")
    return cast(dict[str, object], response)


def _list_response(response: object) -> list[dict[str, object]]:
    """Validate the outer shape of a list of API objects."""
    if not isinstance(response, list):
        raise OceaApiError("Invalid API list response")
    return [_object_response(item) for item in response]


def _validated_response(
    response: dict[str, object],
    validate: Callable[[dict[str, object]], TypeGuard[_ResponseT]],
) -> _ResponseT:
    """Return only responses whose known fields have been validated."""
    if not validate(response):
        raise OceaApiError("Invalid API response")
    return response
