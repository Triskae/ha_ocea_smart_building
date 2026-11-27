# Comfort measurements

The portal has two separate dwelling-level reads: `getComfortMeasurements`
for a dated series and `getDashboardMeasurements` for a dashboard summary.
Neither is implemented by the Python client, and neither appears in the
supplied offline captures. The following describes portal code, not observed
server responses or a complete API contract.

## Availability

The portal maps the dwelling's `fluidesRestitues` to available fluids. It
offers comfort views for `Temperature` and `Hygrometrie` when the corresponding
fluid is advertised. This is a UI selection rule, not proof a specific account
can retrieve data, or that a sensor has usable readings.
Consent, role, device availability and server-side restrictions remain to be
established; never bypass access controls.

## Dated series

The portal submits `debut`, `fin` and `granularity` as JSON to
`getComfortMeasurements`, once per selected fluid, then maps the response's
`interieur` items. Its client-side code maps view modes as follows:

| Portal view | Query granularity |
| --- | --- |
| Day | `Hour` |
| Week or month | `Day` |
| Year | `Month` |

The portal's client-side mapping reads each item's `valeurMin`, `valeurAvg`,
`valeurMax`, `datePeriode` and `unite`. The portal rounds `valeurAvg` to one
decimal in this local mapping;
that is not a property of the raw response. The day view graphs the mapped
average; other views graph minimum, average and maximum.
These are measurements, not accumulated consumption. Do not sum them as
meter readings or infer the server's aggregation rules from the UI.

Only `interieur` is read by this portal call site. The availability, type and
nullability of its fields, any exterior measurements, raw units, date boundaries,
server retention and aggregation semantics are unconfirmed. The portal's date
formatting and granularity choices do not establish server-side limits.
The graph labels show temperature in °C and humidity in %, but no supplied
comfort response establishes the raw `unite` values.

## Dashboard summary

The portal calls `getDashboardMeasurements` and maps each result for display.
It reads `fluide`, `moyenne`, `unite` and `dateDerniereReleve`, using fallback
display values for missing data. The dashboard treats these as monthly
comfort summaries; the time of the latest reading is distinct from the period
represented by the average. Neither the response format nor the period
boundaries are established by a capture.

## Future integration work

Temperature and humidity measurements could be useful as Home Assistant
measurements, but this reference does not implement entities or statistics.
First obtain, with appropriate authorization, sanitized observations from an
equipped dwelling for both fluids and the dashboard. Verify units, date
semantics, missing and null values, access restrictions, freshness and the
meaning of each aggregation before choosing entity or statistics behavior.
No captured comfort fixture is currently available.
