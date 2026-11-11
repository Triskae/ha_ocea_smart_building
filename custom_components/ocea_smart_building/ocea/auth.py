"""Azure AD B2C authentication for the Ocea API."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientSession, ClientTimeout
from yarl import URL

from .const import (
    B2C_AUTHORIZE,
    B2C_BASE,
    B2C_CLIENT_ID,
    B2C_REDIRECT_URI,
    B2C_SCOPE,
    B2C_TENANT,
    B2C_TOKEN,
)
from .exceptions import OceaAuthError

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=30)


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OceaAuthentication:
    """Own B2C credentials and tokens, but not the injected session."""

    def __init__(
        self, session: ClientSession, email: str, password: str, user_agent: str
    ) -> None:
        """Initialize authentication state."""
        self._session = session
        self._email = email
        self._password = password
        self._user_agent = user_agent
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._access_token

    async def authenticate(self) -> None:
        """Perform the browser-compatible Azure AD B2C authorization flow."""
        verifier, challenge = _generate_pkce()
        state = base64.urlsafe_b64encode(
            json.dumps({
                "id": secrets.token_hex(16),
                "meta": {"interactionType": "redirect"},
            }).encode()
        ).decode()
        _LOGGER.debug("Auth step 1/4: loading authorize page")
        async with self._session.get(
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
                "nonce": secrets.token_hex(16),
                "state": state,
            },
            headers={"User-Agent": self._user_agent},
            allow_redirects=True,
            timeout=_TIMEOUT,
        ) as response:
            response.raise_for_status()
            html = await response.text()
            final_url = str(response.url)

        self_asserted_path = (
            f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
            "/B2C_1A_SIGNUP_SIGNIN/SelfAsserted"
        )
        csrf = self._session.cookie_jar.filter_cookies(URL(self_asserted_path)).get(
            "x-ms-cpim-csrf"
        )
        if not csrf:
            raise OceaAuthError("No x-ms-cpim-csrf cookie received")
        trans_match = re.search(r'"transId"\s*:\s*"([^"]+)"', html)
        if not trans_match:
            raise OceaAuthError("Could not extract transId from login page")
        trans_id = trans_match.group(1)

        _LOGGER.debug("Auth step 2/4: posting credentials")
        self_asserted_url = URL(
            f"{self_asserted_path}?tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN",
            encoded=True,
        )
        async with self._session.post(
            self_asserted_url,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": B2C_BASE,
                "Referer": final_url,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": self._user_agent,
                "X-CSRF-TOKEN": csrf.value,
                "X-Requested-With": "XMLHttpRequest",
            },
            data={
                "request_type": "RESPONSE",
                "email": self._email,
                "password": self._password,
            },
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise OceaAuthError(f"Login failed: HTTP {response.status}")
            text = await response.text()
        try:
            result = json.loads(text) if text.strip() else {}
        except ValueError:
            result = {}
        if isinstance(result, dict) and result.get("status") and str(result["status"]) != "200":
            raise OceaAuthError("Login rejected: Invalid credentials")

        _LOGGER.debug("Auth step 3/4: fetching authorization code")
        confirmed_url = URL(
            f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
            f"/B2C_1A_SIGNUP_SIGNIN/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=false&csrf_token={csrf.value}"
            f"&tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN",
            encoded=True,
        )
        async with self._session.get(
            confirmed_url,
            headers={"Referer": final_url, "User-Agent": self._user_agent},
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as response:
            location = response.headers.get("Location", "")
        if not location:
            raise OceaAuthError("No redirect after login confirmation")
        code = None
        if "#" in location:
            fragment = parse_qs(location.split("#", 1)[1])
            code = fragment.get("code", [None])[0]
            if fragment.get("error"):
                raise OceaAuthError("B2C authorization failed")
        if not code:
            code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if not code:
            raise OceaAuthError("No authorization code in redirect")

        _LOGGER.debug("Auth step 4/4: exchanging code for tokens")
        async with self._session.post(
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
            headers=self._token_headers(),
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise OceaAuthError(f"Token exchange failed: HTTP {response.status}")
            tokens: object = await response.json(content_type=None)
        self._store_tokens(tokens, refresh=False)
        _LOGGER.info("Ocea authentication successful")

    async def refresh_access_token(self) -> None:
        """Refresh the access token or perform full authentication."""
        if not self._refresh_token:
            _LOGGER.debug("No refresh token, doing full auth")
            await self.authenticate()
            return
        async with self._session.post(
            B2C_TOKEN,
            data={
                "client_id": B2C_CLIENT_ID,
                "scope": B2C_SCOPE,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
                "client_info": "1",
            },
            headers=self._token_headers(),
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                _LOGGER.warning("Refresh failed (HTTP %s), doing full auth", response.status)
                self._refresh_token = None
                retry = True
                tokens: object = None
            else:
                retry = False
                tokens = await response.json(content_type=None)
        if retry:
            await self.authenticate()
            return
        self._store_tokens(tokens, refresh=True)

    def _store_tokens(self, tokens: object, *, refresh: bool) -> None:
        """Validate and retain tokens without exposing response data."""
        if not isinstance(tokens, dict):
            raise OceaAuthError("Invalid token response")
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            message = "No access token on refresh" if refresh else "No access token received"
            raise OceaAuthError(message)
        refresh_token = tokens.get("refresh_token")
        self._access_token = access
        if isinstance(refresh_token, str) and refresh_token:
            self._refresh_token = refresh_token
        elif not refresh:
            self._refresh_token = None

    def _token_headers(self) -> dict[str, str]:
        """Return browser-compatible headers for token requests."""
        return {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Origin": B2C_REDIRECT_URI,
            "Referer": f"{B2C_REDIRECT_URI}/",
            "User-Agent": self._user_agent,
        }
