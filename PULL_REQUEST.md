# Pull request proposal

## Suggested title

Add detailed consumption, leak estimates, history import, and dwelling recovery

## Summary

Ocea resident accounts may list several occupations, including a stale dwelling
whose consumption dashboard returns HTTP 403. The integration previously stored
the first `logementId` without validating it, which could leave setup or refresh
permanently broken even when another occupation was accessible.

This change:

- keeps the HTTP status on `OceaApiError`;
- validates candidate dwellings through their consumption dashboard;
- tries the currently configured dwelling first when possible;
- skips only HTTP 403 candidates and preserves other API failures;
- updates the stored dwelling during reauthentication or reconfiguration;
- maps a refresh-time HTTP 403 to Home Assistant reauthentication;
- preserves hidden config-entry data through `data_updates`;
- adds French and English messages for accounts with no accessible dwelling.
- exposes precise cold-water, hot-water, and CETC heating totals;
- exposes leak estimates when returned by Ocea;
- imports all available daily history into Recorder on first setup.

## Privacy and compatibility

- No resident, dwelling, email, token, cookie, password, or captured API data is
  included.
- The dwelling identifier remains automatically discovered and is not exposed as
  a user-editable field.
- Azure AD B2C, PKCE, cookies, User-Agent, polling interval, HACS structure, and
  manifest dependencies are unchanged.
- The native Home Assistant Reconfigure flow remains intact.

## Test plan

- Account with one accessible dwelling.
- Account whose first dwelling returns 403 and whose second dwelling works.
- Account where every dwelling returns 403.
- Reauthentication replaces a stale dwelling while preserving hidden entry data.
- Reconfiguration keeps the current dwelling when it remains accessible.
- Refresh-time 403 raises `ConfigEntryAuthFailed`.
- Non-403 API failures are not mistaken for authentication failures.
- Detailed cold-water, hot-water, and CETC responses create precise sensors.
- Leak sensors are created only when `fuiteEstimee` is present.
- First setup imports daily history and marks the import complete only on success.
