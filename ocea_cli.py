#!/usr/bin/env python3
"""
CLI script to authenticate with Ocea Smart Building and fetch water consumption.

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
from urllib.parse import parse_qs, urlparse

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

    # Debug cookies
    for name, value in session.cookies.items():
        log.debug("  Cookie: %s = %s…", name, value[:60])

    # CSRF from cookie
    csrf = session.cookies.get("x-ms-cpim-csrf")
    if not csrf:
        log.error("No x-ms-cpim-csrf cookie found!")
        log.error("All cookies: %s", dict(session.cookies))
        raise SystemExit(1)
    log.info("  ✓ Got CSRF from cookie")
    log.debug("  csrf=%s…", csrf[:50])

    # transId from HTML
    trans_match = re.search(r'"transId"\s*:\s*"([^"]+)"', html)
    if not trans_match:
        log.error("Could not find transId. HTML snippet:\n%s", html[:3000])
        raise SystemExit(1)
    trans_id = trans_match.group(1)
    log.info("  ✓ Got transId from HTML")
    log.debug("  transId=%s", trans_id)

    # ── Step 2: POST credentials ──────────────────────────────────────────
    log.info("Step 2/4 — Posting credentials...")

    # Build the full URL with raw query string to avoid encoding issues
    self_asserted_url = (
        f"{B2C_BASE}/{B2C_TENANT}.onmicrosoft.com"
        f"/B2C_1A_SIGNUP_SIGNIN/SelfAsserted"
        f"?tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
    )

    log.debug("  URL: %s", self_asserted_url)

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

    # Log the exact request being sent
    log.debug("  Prepared URL: %s", prepared.url)
    log.debug("  Prepared headers: %s", dict(prepared.headers))
    log.debug("  Prepared body: %s",
              prepared.body.replace(password, "***") if prepared.body else "(empty)")

    resp = session.send(prepared, allow_redirects=False)

    log.debug("  SelfAsserted HTTP %d", resp.status_code)
    log.debug("  Response headers: %s", dict(resp.headers))
    log.debug("  Response body: %s", resp.text[:500])

    if resp.status_code != 200:
        log.error("Login failed — HTTP %d:\n%s", resp.status_code, resp.text[:500])
        raise SystemExit(1)

    try:
        result = resp.json() if resp.text.strip() else {}
    except json.JSONDecodeError:
        result = {}

    status = result.get("status")
    if status and str(status) != "200":
        msg = result.get("message", resp.text[:200])
        log.error("Login rejected (status=%s): %s", status, msg)
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
    log.debug("  Location: %s", location[:200] if location else "(empty)")

    if not location:
        log.error("No redirect (HTTP %d):\n%s", resp.status_code, resp.text[:500])
        raise SystemExit(1)

    # Parse code from fragment
    code = None
    if "#" in location:
        fragment = location.split("#", 1)[1]
        fparams = parse_qs(fragment)
        code = fparams.get("code", [None])[0]
        error = fparams.get("error", [None])[0]
        if error:
            log.error("B2C error: %s — %s",
                      error, fparams.get("error_description", [""])[0])
            raise SystemExit(1)
    if not code:
        qparams = parse_qs(urlparse(location).query)
        code = qparams.get("code", [None])[0]

    if not code:
        log.error("No auth code in redirect:\n  %s", location[:300])
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
        log.error("Token exchange failed — HTTP %d:\n%s",
                  resp.status_code, resp.text[:500])
        raise SystemExit(1)

    tokens = resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        log.error("No access_token: %s", json.dumps(tokens, indent=2)[:500])
        raise SystemExit(1)

    log.info("  ✓ Got access_token (%d chars) + refresh_token (%s)",
             len(access_token),
             f"{len(refresh_token)} chars" if refresh_token else "none")

    return tokens


# ─── API helpers ──────────────────────────────────────────────────────────────

def api_get(
    session: requests.Session,
    access_token: str,
    path: str,
) -> requests.Response:
    """Make an authenticated GET request, return raw response."""
    url = f"{API_BASE}{path}"
    return session.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Origin": B2C_REDIRECT_URI,
            "Referer": f"{B2C_REDIRECT_URI}/",
            "User-Agent": UA,
        },
    )


def fetch_resident(
    session: requests.Session,
    access_token: str,
) -> dict:
    """Fetch resident info including occupations (logementId)."""
    resp = api_get(session, access_token, "/api/v1/resident")
    if resp.status_code != 200:
        log.error("Resident API failed — HTTP %d:\n%s", resp.status_code, resp.text[:500])
        raise SystemExit(1)
    return resp.json()


def fetch_consumptions(
    session: requests.Session,
    access_token: str,
    local_id: str,
) -> list[dict]:
    resp = api_get(session, access_token, f"/api/v1/local/{local_id}/dashboard/consos")
    if resp.status_code != 200:
        log.error("API failed — HTTP %d:\n%s", resp.status_code, resp.text[:500])
        raise SystemExit(1)
    return resp.json()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    session = requests.Session()

    tokens = authenticate(session, args.email, args.password)

    print("\n" + "=" * 60)
    print("AUTH OK")
    print("=" * 60)

    if args.dump_tokens:
        print("\naccess_token:")
        print(tokens["access_token"][:80] + "…")
        if tokens.get("refresh_token"):
            print("\nrefresh_token:")
            print(tokens["refresh_token"][:80] + "…")

    # Auto-discover local_id from /api/v1/resident
    resident_data = fetch_resident(session, tokens["access_token"])

    resident = resident_data.get("resident", {})
    prenom = resident.get("prenom", "?")
    nom = resident.get("nom", "?")
    print(f"\nResident: {prenom} {nom}")

    occupations = resident_data.get("occupations", [])
    if not occupations:
        log.error("No occupations found for this account!")
        raise SystemExit(1)

    local_id = str(occupations[0].get("logementId", ""))
    if not local_id:
        log.error("No logementId in first occupation!")
        raise SystemExit(1)

    print(f"Local ID: {local_id}")

    if len(occupations) > 1:
        print(f"\nNote: {len(occupations)} occupations found, using the first one.")
        for i, occ in enumerate(occupations):
            print(f"  [{i}] logementId={occ.get('logementId')} — {occ.get('adresse', '?')}")

    print(f"\nFetching consumption for local_id={local_id}…\n")
    data = fetch_consumptions(session, tokens["access_token"], local_id)

    print(json.dumps(data, indent=2, ensure_ascii=False))

    print("\n" + "─" * 40)
    for item in data:
        fluide = item.get("fluide", "?")
        valeur = item.get("valeur", "?")
        unite = item.get("unite", "?")
        label = (
            "🔵 Eau froide" if fluide == "EauFroide"
            else "🔴 Eau chaude" if fluide == "EauChaude"
            else fluide
        )
        print(f"  {label}: {valeur} {unite}")
    print("─" * 40)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch water consumption from Ocea Smart Building",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--dump-tokens", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    main(args)
