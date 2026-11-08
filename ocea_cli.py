#!/usr/bin/env python3
"""
CLI script to authenticate with OCEA Smart Building and explore consumption.

Usage:
    python3 ocea_cli.py --email your@email.com --password yourpass

Dependencies:
    pip3 install requests
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from urllib.parse import parse_qs, quote, urlparse

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

B2C_TENANT = "osbespaceresident"
B2C_CLIENT_ID = "1cacfb15-0b3c-42cc-a662-736e4737e7d9"
B2C_SCOPE = (
    "https://osbespaceresident.onmicrosoft.com/"
    "app-imago-espace-resident-back-prod/user_impersonation "
    "openid profile offline_access"
)
B2C_REDIRECT_URI = "https://espace-resident.ocea-sb.com"

B2C_BASE = f"https://{B2C_TENANT}.b2clogin.com"
B2C_TENANT_PATH = f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
B2C_AUTHORIZE = f"{B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/authorize"
B2C_TOKEN = f"{B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/token"

API_BASE = "https://espace-resident-api.ocea-sb.com"

CONSUMPTION_FLUIDS = frozenset(
    {"EauFroide", "EauChaude", "Rfc", "Cetc", "Cetf", "Electricite", "Gaz"}
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

log = logging.getLogger("ocea")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ─── Auth flow ────────────────────────────────────────────────────────────────

def authenticate(
    session: requests.Session,
    email: str,
    password: str,
) -> dict:
    """Full B2C auth flow using requests.Session for cookie persistence."""

    verifier, challenge = generate_pkce()
    nonce = secrets.token_hex(16)
    state = base64.urlsafe_b64encode(
        json.dumps({
            "id": secrets.token_hex(16),
            "meta": {"interactionType": "redirect"},
        }).encode()
    ).decode()

    # ── Step 1: GET authorize page ────────────────────────────────────────
    log.info("Step 1/4 — Loading authorize page...")

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
        allow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.text
    final_url = resp.url

    # CSRF from cookie
    csrf = session.cookies.get("x-ms-cpim-csrf")
    if not csrf:
        log.error("No x-ms-cpim-csrf cookie found!")
        raise SystemExit(1)
    log.info("  ✓ Got CSRF from cookie")

    # transId from HTML
    trans_match = re.search(r'"transId"\s*:\s*"([^"]+)"', html)
    if not trans_match:
        log.error("Could not find transId in the login page")
        raise SystemExit(1)
    trans_id = trans_match.group(1)
    log.info("  ✓ Got transId from HTML")

    # ── Step 2: POST credentials ──────────────────────────────────────────
    log.info("Step 2/4 — Posting credentials...")

    # Build the full URL with raw query string to avoid encoding issues
    self_asserted_url = (
        f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
        f"/B2C_1A_SIGNUP_SIGNIN/SelfAsserted"
        f"?tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
    )

    # Use a PreparedRequest to control exactly what gets sent
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
            "Sec-GPC": "1",
            "User-Agent": UA,
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Not:A-Brand";v="99", "Brave";v="145", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        },
        data={
            "request_type": "RESPONSE",
            "email": email,
            "password": password,
        },
    )
    prepared = session.prepare_request(req)

    resp = session.send(prepared, allow_redirects=False)

    log.debug("  SelfAsserted HTTP %d", resp.status_code)

    if resp.status_code != 200:
        log.error("Login failed — HTTP %d", resp.status_code)
        raise SystemExit(1)

    try:
        result = resp.json() if resp.text.strip() else {}
    except json.JSONDecodeError:
        result = {}

    if not isinstance(result, dict):
        log.error("Invalid login response")
        raise SystemExit(1)
    status = result.get("status")
    if status and str(status) != "200":
        log.error("Login rejected")
        raise SystemExit(1)

    log.info("  ✓ Credentials accepted")

    # ── Step 3: GET confirmed → authorization code ────────────────────────
    log.info("Step 3/4 — Fetching authorization code...")

    confirmed_url = (
        f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
        f"/B2C_1A_SIGNUP_SIGNIN/api/CombinedSigninAndSignup/confirmed"
        f"?rememberMe=false&csrf_token={csrf}&tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
    )

    resp = session.get(
        confirmed_url,
        headers={"Referer": final_url, "User-Agent": UA},
        allow_redirects=False,
    )

    location = resp.headers.get("Location", "")
    log.debug("  Confirmed HTTP %d", resp.status_code)
    if not location:
        log.error("No redirect (HTTP %d)", resp.status_code)
        raise SystemExit(1)

    # Parse code from fragment
    code = None
    if "#" in location:
        fragment = location.split("#", 1)[1]
        fparams = parse_qs(fragment)
        code = fparams.get("code", [None])[0]
        error = fparams.get("error", [None])[0]
        if error:
            log.error("B2C authorization failed")
            raise SystemExit(1)
    if not code:
        qparams = parse_qs(urlparse(location).query)
        code = qparams.get("code", [None])[0]

    if not code:
        log.error("No auth code in redirect")
        raise SystemExit(1)

    log.info("  ✓ Got authorization code (%d chars)", len(code))

    # ── Step 4: Exchange code for tokens ──────────────────────────────────
    log.info("Step 4/4 — Exchanging code for tokens...")

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
    )

    if resp.status_code != 200:
        log.error("Token exchange failed — HTTP %d", resp.status_code)
        raise SystemExit(1)

    try:
        tokens = resp.json()
    except requests.exceptions.JSONDecodeError:
        log.error("Invalid token response")
        raise SystemExit(1) from None
    if not isinstance(tokens, dict):
        log.error("Invalid token response")
        raise SystemExit(1)
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not isinstance(access_token, str) or not access_token:
        log.error("No access_token in token response")
        raise SystemExit(1)
    if refresh_token is not None and not isinstance(refresh_token, str):
        log.error("Invalid refresh token in token response")
        raise SystemExit(1)

    log.info("  ✓ Got access_token (%d chars) + refresh_token (%s)",
             len(access_token),
             f"{len(refresh_token)} chars" if refresh_token else "none")

    return tokens


# ─── API helpers ──────────────────────────────────────────────────────────────

class OCEACliError(Exception):
    """An API error safe to display without account details."""


def api_request(
    session: requests.Session,
    access_token: str,
    method: str,
    path: str,
    payload: dict[str, str] | None = None,
) -> object:
    """Make an authenticated API request without exposing response bodies."""
    url = f"{API_BASE}{path}"
    response = session.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Origin": B2C_REDIRECT_URI,
            "Referer": f"{B2C_REDIRECT_URI}/",
            "User-Agent": UA,
        },
        json=payload,
        timeout=30,
    )
    log.debug("API %s returned HTTP %d", method, response.status_code)
    if response.status_code != 200:
        raise OCEACliError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        raise OCEACliError("Invalid JSON response") from None


def fetch_resident(
    session: requests.Session,
    access_token: str,
) -> dict:
    """Fetch resident info including occupations (logementId)."""
    log.debug("Requesting resident occupations")
    result = api_request(session, access_token, "GET", "/api/v1/resident")
    if not isinstance(result, dict):
        raise OCEACliError("Invalid resident response")
    return result


def fetch_consumptions(
    session: requests.Session,
    access_token: str,
    local_id: str,
) -> list[dict]:
    log.debug("Requesting dashboard totals")
    result = api_request(
        session, access_token, "GET",
        f"/api/v1/local/{quote(local_id, safe='')}/dashboard/consos",
    )
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise OCEACliError("Invalid dashboard response")
    return result


def fetch_dwelling_fluids(
    session: requests.Session, access_token: str, local_id: str
) -> list:
    """Discover the dwelling's advertised fluids independently of its PDS."""
    result = api_request(
        session, access_token, "GET", f"/api/v1/local/{quote(local_id, safe='')}"
    )
    if not isinstance(result, dict) or not isinstance(result.get("fluidesRestitues"), list):
        raise OCEACliError("Invalid dwelling fluids response")
    return result["fluidesRestitues"]


def fetch_dwelling_consumption(
    session: requests.Session,
    access_token: str,
    local_id: str,
    fluid: str,
    payload: dict[str, str],
) -> object:
    """Fetch consumption for a dwelling and fluid."""
    return api_request(
        session,
        access_token,
        "POST",
        f"/api/v1/local/{quote(local_id, safe='')}/conso/{quote(fluid, safe='')}",
        payload,
    )


def fetch_pds(session: requests.Session, access_token: str, local_id: str) -> list:
    """Discover all measurement points for the selected dwelling."""
    log.debug("Discovering measurement points")
    result = api_request(
        session, access_token, "GET", f"/api/v1/local/{quote(local_id, safe='')}/pds"
    )
    if not isinstance(result, list):
        raise OCEACliError("Invalid PDS response")
    return result


def fetch_pds_consumption(
    session: requests.Session,
    access_token: str,
    local_id: str,
    pds_id: str,
    payload: dict[str, str],
) -> object:
    """Fetch the requested daily-granularity readings for one PDS."""
    return api_request(
        session,
        access_token,
        "POST",
        f"/api/v1/local/{quote(local_id, safe='')}/pds/{quote(pds_id, safe='')}/conso",
        payload,
    )


def date_argument(value: str) -> date:
    """Parse an ISO calendar date supplied on the command line."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid calendar date") from None


def period_payload(start: date, end: date) -> dict[str, str]:
    """Build inclusive daily-granularity bounds in the portal's format."""
    return {
        "debut": f"{start.isoformat()}T00:00:00.000Z",
        "fin": f"{end.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def display_number(value: object) -> str:
    """Validate a reading without printing arbitrary API strings."""
    if value is None:
        return "unavailable"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OCEACliError("Invalid consumption value")
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise OCEACliError("Invalid consumption value") from None
    if not number.is_finite():
        raise OCEACliError("Invalid consumption value")
    return str(number)


def display_unit(value: object) -> str:
    """Display recognized units without emitting arbitrary API data."""
    return value if isinstance(value, str) and value in (
        "m3", "m³", "kWh", "MWh", "Wh", "kW", "L", "l", "U", "°C", "%"
    ) else "unknown unit"


def display_consumptions(response: object, start: date, end: date, today: date) -> None:
    """Display actual readings without assuming the API honored the granularity."""
    if not isinstance(response, dict) or not isinstance(response.get("consommations"), list):
        raise OCEACliError("Invalid consumption response")
    unit = display_unit(response.get("unite"))
    items = response["consommations"]
    log.debug("Received %d consumption readings", len(items))
    if not items:
        if start >= today:
            print("  No completed days in this period; daily readings are available the following day.")
        else:
            print("  No readings returned for this period.")
        return
    readings = []
    for item in items:
        if not isinstance(item, dict):
            raise OCEACliError("Invalid consumption item")
        raw_date = item.get("date")
        if not isinstance(raw_date, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?",
            raw_date,
        ):
            raise OCEACliError("Invalid consumption date")
        try:
            reading_date = datetime.fromisoformat(raw_date).date()
        except ValueError:
            raise OCEACliError("Invalid consumption date") from None
        value = display_number(item.get("valeur"))
        extras = []
        for key, label in (("valeurCoefficientee", "corrected"), ("fuiteEstimee", "estimated leak")):
            if key in item:
                extras.append(f"{label}: {display_number(item[key])} {unit}")
        for key, label in (("isEstimated", "estimated"), ("consentement", "consent")):
            if key in item:
                if not isinstance(item[key], bool):
                    raise OCEACliError("Invalid consumption metadata")
                extras.append(f"{label}: {'yes' if item[key] else 'no'}")
        if "type" in item:
            response_type = item["type"]
            if response_type not in (
                "ConsommationResponse", "ConsommationEauResponse", "ConsommationRfcResponse"
            ):
                response_type = "other"
            extras.append(f"type: {response_type}")
        readings.append((reading_date, raw_date, value, extras))
    for _, raw_date, value, extras in readings:
        detail = f" ({', '.join(extras)})" if extras else ""
        print(f"  {raw_date}: {value} {unit}{detail}")
    dates = sorted({reading[0] for reading in readings if start <= reading[0] <= end})
    completed_end = min(end, today - timedelta(days=1))
    expected_days = max(0, (completed_end - start).days + 1)
    if sum(day <= completed_end for day in dates) < expected_days:
        print("  Some dates in the requested period have no reading; the cause is unknown.")
    if len(dates) >= 2 and all(day.day == 1 for day in dates) and all(
        (right - left).days >= 28 for left, right in pairwise(dates)
    ):
        print("  Returned dates appear monthly; daily availability is not confirmed.")


def display_dwelling_consumptions(
    response: object, start: date, end: date, today: date
) -> None:
    """Describe the safe, known fields of an aggregated consumption response."""
    if not isinstance(response, dict) or not isinstance(response.get("consommations"), list):
        raise OCEACliError("Invalid consumption response")
    items = response["consommations"]
    if not all(isinstance(item, dict) for item in items):
        raise OCEACliError("Invalid consumption item")
    known_fields = (
        "date", "valeur", "isEstimated", "consentement", "type",
        "fuiteEstimee", "valeurCoefficientee",
    )
    print(f"  Reported unit: {display_unit(response.get('unite'))}")
    print(f"  Readings returned: {len(items)}")
    if items:
        for key in known_fields:
            print(f"  Field {key}: {sum(key in item for item in items)}/{len(items)}")
        unknown = sum(bool(set(item) - set(known_fields)) for item in items)
        if unknown:
            print(f"  Readings with other fields: {unknown}")
    if set(response) - {"unite", "consommations"}:
        print("  Response contains other fields.")
    display_consumptions(response, start, end, today)


# ─── Main ─────────────────────────────────────────────────────────────────────

def fluid_label(value: object) -> str:
    """Display known fluids without emitting arbitrary account data."""
    return {
        "EauFroide": "Cold water",
        "EauChaude": "Hot water",
        "Cetc": "Heating (CETC)",
        "Cetf": "Cooling (CETF)",
        "Rfc": "Heating allocation (RFC)",
        "Gaz": "Gas",
        "Electricite": "Electricity",
        "Fioul": "Fuel oil",
        "Temperature": "Temperature",
        "Hygrometrie": "Humidity",
        "Air": "Air",
        "DeltaP": "Pressure difference",
        "CO2": "CO2",
        "Horaire": "Operating time",
    }.get(value, "Other fluid") if isinstance(value, str) else "Other fluid"


def main(args: argparse.Namespace) -> int:
    """Display dwelling and PDS consumption for the first dwelling."""
    today = datetime.now(timezone.utc).astimezone().date()
    start = args.start_date or today.replace(day=1)
    end = args.end_date or today
    payload = period_payload(start, end)

    with requests.Session() as session:
        tokens = authenticate(session, args.email, args.password)
        access_token = tokens["access_token"]
        print("\nAUTH OK")
        if args.dump_tokens:
            print("\naccess_token:")
            print(access_token[:80] + "…")
            if tokens.get("refresh_token"):
                print("\nrefresh_token:")
                print(tokens["refresh_token"][:80] + "…")

        resident_data = fetch_resident(session, access_token)
        occupations = resident_data.get("occupations")
        if not isinstance(occupations, list) or not occupations or not isinstance(occupations[0], dict):
            raise OCEACliError("No valid occupation found for this account")
        local_id = occupations[0].get("logementId")
        if not isinstance(local_id, (str, int)) or isinstance(local_id, bool) or not str(local_id):
            raise OCEACliError("No dwelling identifier in the first occupation")
        local_id = str(local_id)
        if len(occupations) > 1:
            print(f"Found {len(occupations)} occupations; using the first one.")

        failed = False
        print("\nDashboard totals:")
        try:
            for item in fetch_consumptions(session, access_token, local_id):
                unit = display_unit(item.get("unite"))
                print(f"  {fluid_label(item.get('fluide'))}: {display_number(item.get('valeur'))} {unit}")
        except OCEACliError as err:
            log.error("Dashboard: %s", err)
            failed = True
        except requests.RequestException:
            log.error("Dashboard: network request failed")
            failed = True

        print(f"\nRequested daily granularity: {start.isoformat()} to {end.isoformat()} (inclusive).")
        print("Consumption dates and values are private; review them before sharing.")
        print("\nDwelling consumption:")
        try:
            dwelling_fluids = fetch_dwelling_fluids(session, access_token, local_id)
        except OCEACliError as err:
            log.error("Dwelling fluids: %s", err)
            failed = True
            dwelling_fluids = None
        except requests.RequestException:
            log.error("Dwelling fluids: network request failed")
            failed = True
            dwelling_fluids = None

        seen_fluids: set[str] = set()
        for item in dwelling_fluids or []:
            if not isinstance(item, dict) or not isinstance(item.get("fluide"), str):
                log.error("Dwelling fluids: invalid fluid data")
                failed = True
                continue
            fluid = item["fluide"]
            if fluid not in CONSUMPTION_FLUIDS:
                print("  Skipping an unsupported dwelling fluid.")
                continue
            if fluid in seen_fluids:
                continue
            seen_fluids.add(fluid)
            label = fluid_label(fluid)
            print(f"\nDwelling: {label} ({fluid})")
            try:
                response = fetch_dwelling_consumption(
                    session, access_token, local_id, fluid, payload
                )
                display_dwelling_consumptions(response, start, end, today)
            except OCEACliError as err:
                log.error("Dwelling %s: %s", label, err)
                failed = True
            except requests.RequestException:
                log.error("Dwelling %s: network request failed", label)
                failed = True
        if dwelling_fluids == []:
            print("  No dwelling fluids available.")

        pds_list = fetch_pds(session, access_token, local_id)
        if not pds_list:
            print("No PDS found for this dwelling.")
        for index, pds in enumerate(pds_list, 1):
            print(f"\nPDS {index}: {fluid_label(pds.get('fluide')) if isinstance(pds, dict) else 'unknown fluid'}")
            if not isinstance(pds, dict):
                log.error("PDS %d: invalid PDS data", index)
                failed = True
                continue
            pds_id = pds.get("id")
            if not isinstance(pds_id, (str, int)) or isinstance(pds_id, bool) or not str(pds_id):
                log.error("PDS %d: missing identifier", index)
                failed = True
                continue
            try:
                log.debug("PDS %d: requesting daily-granularity consumption", index)
                response = fetch_pds_consumption(session, access_token, local_id, str(pds_id), payload)
                display_consumptions(response, start, end, today)
            except OCEACliError as err:
                log.error("PDS %d: %s", index, err)
                failed = True
            except requests.RequestException:
                log.error("PDS %d: network request failed", index)
                failed = True
        return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Explore OCEA Smart Building dwelling and measurement-point consumption",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--dump-tokens", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--start-date", type=date_argument, help="First date, inclusive (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date_argument, help="Last date, inclusive (YYYY-MM-DD)")

    args = parser.parse_args()
    if (args.start_date is None) != (args.end_date is None):
        parser.error("--start-date and --end-date must be provided together")
    if args.start_date is not None and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logging.getLogger("urllib3").setLevel(logging.CRITICAL + 1)

    try:
        raise SystemExit(main(args))
    except OCEACliError as err:
        log.error("OCEA API: %s", err)
        raise SystemExit(1) from None
    except requests.RequestException:
        log.error("Network request failed")
        raise SystemExit(1) from None
