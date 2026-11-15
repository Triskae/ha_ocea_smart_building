"""Errors raised by the Ocea API client."""


class OceaAuthError(Exception):
    """Authentication error."""


class OceaApiError(Exception):
    """API communication error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Keep the HTTP status separately from the error message."""
        super().__init__(message)
        self.status = status
