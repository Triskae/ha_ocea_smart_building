# Incomplete data formats

The [OpenAPI document](openapi.yaml) records fields and values supported by
the portal's browser-delivered code, the integration and the supplied
observations. It is not a complete server contract. Examples are not closed
enumerations: a portal declaration does not prove that a value is accepted by
every operation, returned to every account, or exhaustive. A replacement value
in a fixture is not an observation of the original value.

## Types supported by the integration

The schemas describe partial client-side expectations, not an exhaustive
guarantee about server responses. Properties remain optional; accepting `null`
in a schema records a supported or tolerated form, not proof it is always sent.
The integration accepts string, integer or null PDS identifiers and device
`pdsId` values. For supported PDS and matched devices, it accepts string or
null metadata and numeric or null coefficients. The consumption-item
`consentement` is boolean or null; resident and dwelling `consentement` are
separate objects in portal use.

The supplied resident and dwelling observations contain consent objects with
a boolean validity flag and string source and update date. They also show
address components as strings, notification settings as booleans, and PDS
locations as strings. These are forms in an anonymized account-specific
capture, not proof of exhaustive types, presence or nullability. The
integration additionally accepts integers for address components. Dates may
have been replaced or altered during anonymization; no specific server date
format is inferred from them.

The integration parses consumption numbers and decimal strings, including a
comma decimal separator. It parses reading dates as ISO dates or timestamps,
but a `date-time` constraint would exclude date-only values. Invalid or
unsupported values may be treated as unavailable by the integration; this is
not proof that the server sends them. The portal also converts some values to
dates and performs JavaScript arithmetic, but those conversions alone do not
establish the raw JSON types. In particular, unclassified consumption items
can retain an `isEstimated` value the client does not recognize as boolean.

## Known values and presentation

The portal declares the fluid identifiers listed in the OpenAPI `Fluid`
schema. It uses `Temperature` and `Hygrometrie` for comfort and defines the
fluid groups `Eau`, `Chauffage`, `Thermique`, `Electricite`, `Gaz`,
`Temperature` and `Hygrometrie`. The water PDS and dashboard observations
contain `EauFroide` and `m3`. The integration recognizes `m3` for cold and
hot water and `kWh` for `Cetc`; this does not establish all server units.
The portal uses display labels `m3` for volume, `kWh` for energy and `U` for
heating allocators. Its comfort graph labels use °C and % for temperature and
humidity, respectively. Display labels do not establish the raw units returned
by the comfort API or all fluid-to-unit mappings.

The portal declares `Hour`, `Day` and `Month` granularities; supplied PDS
requests show `Day` and `Month`. Comfort uses `Hour` for the day view, `Day`
for week and month views, and `Month` for the year view. These client choices
do not establish the server's allowed combinations or boundaries.

## Remaining gaps

| Data | Known structure and outstanding questions |
| --- | --- |
| Electricity profile, resident links, dwelling devices | Read operations are declared, but their complete response shapes, types and optionality are not established in OpenAPI. |
| Resident, dwelling and site | The portal maps consent objects and lists of occupations and advertised fluids; observations also show some address and notification fields. Complete nested structures, server nullability and required fields still need evidence. |
| PDS and devices | The integration checks selected identifier, location, coefficient and installation-date types. The complete device response, server formats and field presence remain uncertain. Portal reading types do not prove availability or server restrictions. |
| Dashboard consumption | An excerpt has a decimal-comma string and `m3`; the client also parses numbers. Server representations, nulls, other fluids and units, rounding and aggregation remain incomplete. The excerpt's measurement is a replacement, not an actual reading. |
| Consumption requests and series | Date strings and `Day`/`Month` requests are shown in fixtures, but accepted server date formats, time zones, boundary inclusivity, retention and aggregation rules are unknown. The integration parses ISO dates or timestamps and numeric values or strings. Complete item variants, requiredness, nullability and leak-estimate units remain unknown. |
| Comfort series and dashboard | Portal mapping names interior min/average/max and dashboard summary fields, but no supplied comfort capture establishes their full response shape, raw units, numeric/date formats, missing/null behavior, exterior data or aggregation. See [comfort notes](domains/comfort.md). |
| Consumption insights | The portal maps a breakdown list and converts the start value to a date, but raw value types, complete nested structure, units, period definitions and availability are not established. The breakdown is theoretical, not measured per-device usage. |
| Dwelling and threshold alerts | Portal usage identifies some fields and alert values; complete response types, date formats, units, optionality, meanings and account or dwelling scope restrictions need verification. |
| Goals | `eauFroide` and `eauChaude` are named in portal use, but their types, units, requiredness and complete response structure are unknown. |

Future observations should be obtained only for resources the account is
authorized to access, then minimized and anonymized before being recorded.
Do not fabricate fixtures to resolve gaps. See the [fixture policy](README.md#fixtures-and-privacy).
