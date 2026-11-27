# Domain notes

The [OpenAPI reference](../openapi.yaml) and [inventory](../inventory/endpoints.yaml)
are the authoritative operation lists. The fields below are names used by
the portal or integration, not a complete schema or guarantee of availability.

## Resident and dwelling

`getResident` returns resident information, occupations, notification
preferences and consent information. Occupations link an account to dwellings;
do not assume a single dwelling. `getDwelling` contains `local`,
`fluidesRestitues`, `lastOccupation` and `consentement`. `getSite` describes
site configuration. Portal usage and captured HTTP 200 objects agree on these
lookups; neither complete schemas nor every field's presence are guaranteed.
The integration interprets a subset of these structures and does not request
site configuration. The CLI reads resident and dwelling data independently.
Resident goal and electricity-profile reads are discovered but not implemented.

## PDS and devices

`getPds` lists measurement points for a dwelling. `getDevicesByPds` submits
`pdsIds` to look up corresponding device metadata; identifiers and serial
numbers are sensitive. Captured requests and HTTP 200 lists support both the
PDS list and PDS-device lookup; the CLI implements only the former. A
separate dwelling-device read exists in the portal,
but is not implemented in the integration. A PDS is not necessarily identical
to a physical device.

## Consumption and comfort

The portal and integration query series with `debut`, `fin` and `granularity`.
PDS and dwelling consumption are separate scopes and may overlap; never add
them as independent totals without evidence. The offline capture covers PDS
consumption with daily and monthly queries and HTTP 200 JSON responses, not
the dwelling-consumption operation. Portal code uses both for graphing; both
Python clients can request them. The integration uses
daily data; the portal also uses monthly views. The portal uses midnight UTC
for graph bounds, whereas history requests in the integration end at
23:59:59.999Z. Server boundary semantics are not established.

The portal models `ConsommationEauResponse` with `fuiteEstimee` and
`ConsommationRfcResponse` with `valeurCoefficientee`; do not infer the
leak-estimate unit or treat heating allocator values as ordinary energy.
Honor `consentement`, the occupation period and service access restrictions.
The portal limits PDS history display to `Tele` meters; the integration does
not apply that filter. UI limits are not API retention guarantees.

See [comfort measurements](comfort.md) for portal usage, available fields,
granularities and the separate dashboard summary. Complete response schemas
and server behavior remain unknown.
The consumption-insights breakdown is theoretical, not measured per-device
usage. None of these three operations is implemented in the Python client.

## Alerts

Dwelling alert reads and account-level threshold alerts are separate APIs.
The portal filters current dwelling alerts to leaks and uses different lists
for threshold history; role and authorization differences must be respected.
The integration's leak entity instead derives its state from `fuiteEstimee`
in consumption readings and does not query these alert APIs.
