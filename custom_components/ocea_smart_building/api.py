"""API client for Ocea Smart Building — uses requests (sync) for reliable B2C auth."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .const import (
    API_BASE,
    B2C_AUTHORIZE,
    B2C_BASE,
    B2C_CLIENT_ID,
    B2C_REDIRECT_URI,
    B2C_SCOPE,
    B2C_TOKEN,
    B2C_TENANT,
    UA,
)

_LOGGER = logging.getLogger(__name__)


class OceaAuthError(Exception):
    """Authentication error."""


class OceaApiError(Exception):
    """API communication error."""


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OceaApiClient:
    """Client to interact with Ocea Smart Building API.

    Uses requests.Session (sync) because aiohttp mangles the B2C query
    parameters (encoding '=' in the tx StateProperties value).
    HA calls these methods via hass.async_add_executor_job().
    """

    def __init__(
        self,
        email: str,
        password: str,
        local_id: str,
    ) -> None:
        """Initialize the API client."""
        self._email = email
        self._password = password
        self._local_id = local_id
        self._session = requests.Session()
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    def close(self) -> None:
        """Close the requests session."""
        self._session.close()

    # ── Full B2C auth flow ────────────────────────────────────────────────

    def authenticate(self) -> None:
        """Perform full Azure AD B2C authentication flow (4 steps)."""
        session = self._session
        verifier, challenge = _generate_pkce()
        nonce = secrets.token_hex(16)
        state = base64.urlsafe_b64encode(
            json.dumps({
                "id": secrets.token_hex(16),
                "meta": {"interactionType": "redirect"},
            }).encode()
        ).decode()

        # Step 1: GET authorize page → cookies + CSRF + transId
        _LOGGER.debug("Auth step 1/4: loading authorize page")

        resp = session.get(
            B2C_AUTHORIZE,
            params={
                "client_id": B2C_CLIENT_ID,
                "scope": B2C_SCOPE,
                "redirect_uri": B2C_REDIRECT_URI,
                "response_mode": "fragment",
                "response_type": "code",
                "x-client-SKU": "msal.js.browser",
                "x-client-VER": "3.10.0",
                "client_info": "1",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": nonce,
                "state": state,
            },
            headers={"User-Agent": UA},
            allow_redirects=True,
            timeout=30,
        )
        resp.raise_for_status()
        html = resp.text
        final_url = resp.url

        csrf = session.cookies.get("x-ms-cpim-csrf")
        if not csrf:
            raise OceaAuthError("No x-ms-cpim-csrf cookie received")

        trans_match = re.search(r'"transId"\s*:\s*"([^"]+)"', html)
        if not trans_match:
            raise OceaAuthError("Could not extract transId from login page")
        trans_id = trans_match.group(1)

        # Step 2: POST credentials via SelfAsserted
        _LOGGER.debug("Auth step 2/4: posting credentials")

        # Build URL manually to preserve '=' in tx=StateProperties=eyJ...
        self_asserted_url = (
            f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
            f"/B2C_1A_SIGNUP_SIGNIN/SelfAsserted"
            f"?tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
        )

        req = requests.Request(
            method="POST",
            url=self_asserted_url,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": B2C_BASE,
                "Referer": final_url,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": UA,
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
            },
            data={
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            },
        )
        prepared = session.prepare_request(req)
        resp = session.send(prepared, allow_redirects=False, timeout=30)

        if resp.status_code != 200:
            raise OceaAuthError(f"Login failed: HTTP {resp.status_code}")

        try:
            result = resp.json() if resp.text.strip() else {}
        except ValueError:
            result = {}

        if result.get("status") and str(result["status"]) != "200":
            msg = result.get("message", "Invalid credentials")
            raise OceaAuthError(f"Login rejected: {msg}")

        # Step 3: GET confirmed → authorization code in redirect
        _LOGGER.debug("Auth step 3/4: fetching authorization code")

        confirmed_url = (
            f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
            f"/B2C_1A_SIGNUP_SIGNIN/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=false&csrf_token={csrf}"
            f"&tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
        )

        resp = session.get(
            confirmed_url,
            headers={"Referer": final_url, "User-Agent": UA},
            allow_redirects=False,
            timeout=30,
        )

        location = resp.headers.get("Location", "")
        if not location:
            raise OceaAuthError("No redirect after login confirmation")

        code = None
        if "#" in location:
            fragment = location.split("#", 1)[1]
            fparams = parse_qs(fragment)
            code = fparams.get("code", [None])[0]
            error = fparams.get("error", [None])[0]
            if error:
                desc = fparams.get("error_description", [""])[0]
                raise OceaAuthError(f"B2C error: {error} — {desc}")
        if not code:
            qparams = parse_qs(urlparse(location).query)
            code = qparams.get("code", [None])[0]
        if not code:
            raise OceaAuthError("No authorization code in redirect")

        # Step 4: Exchange code for tokens
        _LOGGER.debug("Auth step 4/4: exchanging code for tokens")

        resp = session.post(
            B2C_TOKEN,
            data={
                "client_id": B2C_CLIENT_ID,
                "redirect_uri": B2C_REDIRECT_URI,
                "scope": B2C_SCOPE,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "client_info": "1",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "Origin": B2C_REDIRECT_URI,
                "Referer": f"{B2C_REDIRECT_URI}/",
                "User-Agent": UA,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            raise OceaAuthError(f"Token exchange failed: HTTP {resp.status_code}")

        tokens = resp.json()
        self._access_token = tokens.get("access_token")
        self._refresh_token = tokens.get("refresh_token")

        if not self._access_token:
            raise OceaAuthError("No access token received")

        _LOGGER.info("Ocea authentication successful")

    # ── Token refresh ─────────────────────────────────────────────────────

    def refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token."""
        if not self._refresh_token:
            _LOGGER.debug("No refresh token, doing full auth")
            self.authenticate()
            return

        resp = self._session.post(
            B2C_TOKEN,
            data={
                "client_id": B2C_CLIENT_ID,
                "scope": B2C_SCOPE,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
                "client_info": "1",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "Origin": B2C_REDIRECT_URI,
                "Referer": f"{B2C_REDIRECT_URI}/",
                "User-Agent": UA,
            },
            timeout=30,
        )

        if resp.status_code != 200:
            _LOGGER.warning("Refresh failed (HTTP %s), doing full auth", resp.status_code)
            self._refresh_token = None
            self.authenticate()
            return

        tokens = resp.json()
        self._access_token = tokens.get("access_token")
        new_refresh = tokens.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh

        if not self._access_token:
            raise OceaAuthError("No access token on refresh")

    # ── API requests ──────────────────────────────────────────────────────

    def _api_get(self, path: str, retry_auth: bool = True) -> any:
        """Make an authenticated GET request to the Ocea API."""
        if not self._access_token:
            self.authenticate()

        url = f"{API_BASE}{path}"
        resp = self._session.get(
            url,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Origin": B2C_REDIRECT_URI,
                "Referer": f"{B2C_REDIRECT_URI}/",
                "User-Agent": UA,
            },
            timeout=30,
        )

        if resp.status_code == 401 and retry_auth:
            _LOGGER.debug("Token expired, refreshing")
            self.refresh_access_token()
            return self._api_get(path, retry_auth=False)

        if resp.status_code != 200:
            raise OceaApiError(f"API error: HTTP {resp.status_code} — {resp.text[:200]}")

        return resp.json()

    def get_consumptions(self) -> list[dict[str, str]]:
        """Get water consumption data."""
        return self._api_get(f"/api/v1/local/{self._local_id}/dashboard/consos")

    def validate_credentials(self) -> bool:
        """Test credentials by attempting authentication."""
        try:
            self.authenticate()
            return True
        except OceaAuthError:
            return False
