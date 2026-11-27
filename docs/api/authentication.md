# Authentication

The existing integration uses Azure AD B2C authorization code flow with PKCE.
Its browser-compatible sequence, cookies, redirect parsing and token exchange
are implemented in [`auth.py`](../../custom_components/ocea_smart_building/ocea/auth.py);
the client uses an externally owned HTTP session. Credentials are stored in the
Home Assistant config entry; access and refresh tokens are held in memory.

[`client.py`](../../custom_components/ocea_smart_building/ocea/client.py)
sends the access token as a bearer token, accepts JSON, and sends Origin,
Referer and User-Agent headers. POST requests also use JSON content type. It
refreshes authentication once after HTTP 401 and otherwise reports API errors.
The portal's generated client sends `Accept` and, for JSON POST requests,
`Content-Type`; the shared HTTP client supplies authentication separately.

These are observed implementation details, not a complete server-side
authentication contract. Do not capture or publish authorization codes,
tokens, cookies, credentials, authenticated HTML or account-specific data.
