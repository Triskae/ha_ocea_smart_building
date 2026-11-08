"""OCEA resident API client and data models."""

from .client import OCEAApiClient
from .exceptions import OCEAApiError, OCEAAuthError
from .models import (
    OccupationStatus,
    OCEAAddress,
    OCEADwelling,
    OCEADwellingDetails,
    OCEAMeter,
    OCEAPds,
    OCEAResident,
)

__all__ = [
    "OCEAAddress",
    "OCEAApiClient",
    "OCEAApiError",
    "OCEAAuthError",
    "OCEADwelling",
    "OCEADwellingDetails",
    "OCEAMeter",
    "OCEAPds",
    "OCEAResident",
    "OccupationStatus",
]
