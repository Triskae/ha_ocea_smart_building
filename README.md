# Ocea Smart Building - Home Assistant Integration

[![marketplace][marketplace-badge]][marketplace-url]
[![release][release-badge]][release-url]
[![installations][installations-badge]][installations-url]

Home Assistant integration to monitor water and heating consumption from the [Ocea Smart Building](https://espace-resident.ocea-sb.com) resident portal.

## Features

- Dated cold- and hot-water consumption statistics in m³ for each PDS
  (point of measurement)
- Separate dated cold- and hot-water consumption statistics in m³ per dwelling
- Dated **CETC heating** statistics in kWh per PDS and dwelling (`Cetc` in
  the Ocea API), based on the resident portal's fluid and unit mapping
- Leak detection for each individual water meter
- Water statistics are compatible with the **Energy dashboard**
- Automatic Azure AD B2C authentication (no headless browser needed)
- Automatic token refresh
- UI-based configuration
- Native Home Assistant reconfiguration for the email and password

## Installation

### Marketplace (Home Assistant 2026.11 and above, recommended)

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=Triskae&repository=ha_ocea_smart_building&category=integration)

Since Home Assistant 2026.11, HACS is built into Home Assistant as the
[Marketplace](https://www.home-assistant.io/integrations/marketplace/), found
under **Settings** → **Marketplace**. This integration is available in its
catalog.

1. Open the integration with the button above, or search for **Ocea Smart Building** under **Settings** → **Marketplace**
2. If this is your first time using the Marketplace, read the notice and accept it
3. Select **Install**, which takes you straight into the [configuration](#configuration)

If you previously installed the integration with HACS, Home Assistant migrates
it to the Marketplace automatically when you upgrade to 2026.11. You do not
need to reinstall it.

### HACS (Home Assistant 2026.10 and below)

On earlier versions, install the integration with HACS, where it is available
in the default repository:

1. [Open the repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=Triskae&repository=ha_ocea_smart_building&category=integration)
2. Select **Download**
3. Restart Home Assistant

### Manual

Copy `custom_components/ocea_smart_building/` into `config/custom_components/` and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and add the integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ocea_smart_building)

Settings → Devices & Services → Add Integration → "Ocea Smart Building"

- **Email**: your Ocea resident portal email
- **Password**: your Ocea password

If your account has one accessible dwelling, it is selected automatically. If
several are accessible, choose the dwelling to monitor. Add the integration again
to monitor another dwelling with the same account. Each dwelling can only be
added once. The displayed occupation status is indicative, based on the dates
supplied by Ocea, and does not determine access.

The integration's System Health page aggregates update status and per-meter
reading availability across all loaded entries. After a failed update, cached
readings for that entry are counted as unavailable until its next successful update.

### Water meters

The integration imports an external statistic for each cold- or hot-water PDS.
Water consumption is not exposed as a sensor. Leak entities remain available
per water meter.

When the detailed Ocea response includes `fuiteEstimee`, an additional
`Fuite estimée` sensor is created for that meter. It uses m³ and represents the
latest leak estimate reported by Ocea; it is not created when Ocea provides no
leak estimate. If Ocea starts reporting a leak after setup, the entity is added
automatically during the next coordinator update; no integration reload is
needed.

The integration also creates a `Fuite détectée` binary sensor for every
individual water meter. It is `off` when Ocea reports an estimated leak of
`0`, and `on` when the estimate is greater than `0`. If Ocea does not provide
an estimate, the binary sensor is temporarily unavailable rather than being
treated as `off`.

Use this binary sensor in automations, for example:

```yaml
alias: Alerte fuite d'eau Ocea
trigger:
  - platform: state
    entity_id: binary_sensor.ocea_smart_building_compteur_cuisine_fuite_detectee
    from: "off"
    to: "on"
action:
  - service: notify.mobile_app_telephone
    data:
      title: Fuite d'eau détectée
      message: Le compteur Ocea signale une fuite estimée.
```

The integration polls Ocea every five hours. Leak detection therefore follows
that refresh interval and is not real-time.

If Ocea returns an invalid or incomplete dated reading, the coordinator skips
it and logs a warning; its meter total can therefore be incomplete. The history
synchronizer does not advance over a response containing invalid readings and
retries that period later.

The integration discovers the available PDS and meter serial numbers during
setup, then refreshes each meter's consumption for the current calendar month.
If one meter is temporarily unavailable, its leak entities remain present but
unavailable until a later refresh succeeds; other meters continue to update.
The discovery list is cached while the integration is loaded. Reload
the integration after a meter is replaced, added, or removed in the Ocea portal
so Home Assistant can synchronize the meter entities.

### Developer structure

See [docs/architecture.md](docs/architecture.md) for module responsibilities,
data flow, and contributor-facing design invariants.

### Manual consumption explorer

The standalone CLI uses Python and `requests`, without Home Assistant:

Run the following commands in Bash. If your terminal uses another shell, such
as zsh, run `bash` first; the password prompt below uses Bash-specific options.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install requests
read -r -s -p "Ocea password: " OCEA_PASSWORD; echo
.venv/bin/python ocea_cli.py --email "YOUR_EMAIL" --password "$OCEA_PASSWORD" --verbose
unset OCEA_PASSWORD
```

Replace `YOUR_EMAIL` with your Ocea login. The password is not typed into shell
history, but it can be visible to other users through the process arguments
while the command runs. Use a trusted machine.

The CLI displays dashboard totals, requests dwelling-level consumption for
each supported fluid advertised by the first dwelling, and queries every
measurement point (PDS), including heating and other fluids. It reports the
unit, presence of known fields, and actual readings for each dwelling fluid;
unexpected fields are counted without displaying their names or values. By
default it requests daily granularity from the start of the current month
through today.
To choose an inclusive interval, add both `--start-date YYYY-MM-DD` and
`--end-date YYYY-MM-DD`. The default dates use the machine's local calendar;
the API request expresses each bound at the start or end of the day in UTC.
`--verbose` adds sanitized request traces.

The API may return no readings or readings spaced by months even when `Day` is
requested; the CLI displays only what Ocea actually returns. Daily readings
are available the following day, so today and future dates are excluded when
checking for missing readings. A failed PDS is reported without stopping the
others, and causes a nonzero exit status. Failure to discover dwelling fluids
or fetch one dwelling series is also reported without stopping PDS exploration.
This tool does not import historical data into Home Assistant.

Dates and consumption values are private even when dwelling and PDS identifiers
are omitted. Review and anonymize output before sharing it; do not commit it to
the repository. Do not share token output:
`--dump-tokens` deliberately displays parts of the authentication tokens.

### Reconfiguration

Open Settings → Devices & Services → Ocea Smart Building, then select the native **Reconfigure** action.

- The current email address is prefilled and can be changed.
- The stored password is never displayed. Leave the password field unchanged to keep it, or enter a new password to replace it.
- Credentials are validated with Ocea before changes are saved.
- Home Assistant reloads the integration only when the validated configuration has actually changed.

If Home Assistant requests reauthentication, sign in with an Ocea account that
can access the configured dwelling. A successful
reauthentication also refreshes the entry name from the account. Both
reauthentication and reconfiguration retain the selected dwelling. If it is
missing or no longer accessible, restore access with Ocea and retry.

To monitor a different dwelling, add another integration entry. Removing an
entry also removes its devices and entities: review automations, dashboards and
other references before removal. Imported Recorder statistics are not deleted.

## Energy dashboard

In Settings → Dashboards → Energy → Water consumption, select the Ocea
external statistics for either the dwelling or its meters. Do not select both
for the same water consumption: the dwelling series can include those meters
and would be counted twice. Replace the old Ocea
sensor sources instead of adding both: otherwise water may be counted twice.
Home Assistant does not change existing Energy dashboard settings automatically.
The previous water consumption entities are no longer provided; check dashboards
and automations that referenced them. Their existing Recorder history is not
deleted or migrated into the new statistics.

The integration progressively fetches daily dwelling water and CETC readings
from the occupation start date, and water and CETC PDS readings from the later of the
occupation start date and the current meter's installation date, up to 30 days
per series and five-hour cycle. The dwelling history is requested independently
of its meters and becomes current as backfill progresses. A unique current
occupation is required; missing
or ambiguous start dates prevent backfill. Missing readings are
not estimated. Once backfill is complete, the integration rechecks imported
history in rotating periods of up to 30 days, subject to the same five-hour
request limit. A correction is applied when its period is rechecked; the time
for a full pass depends on the amount of history. An omitted date is not treated
as a zero or as a deletion. Earlier readings from a replaced meter are not
requested from Ocea.

Daily dates use the Ocea calendar (`Europe/Paris`) and are assigned to the last
full hour of that day; they are not hourly measurements. The Home Assistant
timezone does not affect the statistic ID or the day assigned to a reading. A
PDS statistic series is identified by the dwelling, PDS and exact Ocea fluid. A
replacement device using the same PDS therefore continues that PDS series; a
different PDS creates a different series. Dwelling series have separate stable
identifiers derived from the dwelling and fluid. Statistic display names include
the dwelling identifier, which is visible in Home Assistant and stored in
Recorder metadata. Existing PDS statistic identifiers and history are retained.

Backfill progress and readings are cached privately in Home Assistant. If the
cache is missing or invalid, history is reconstructed under the same statistic
ID; existing Recorder statistics are neither reset nor deleted. Removing an
entry deletes its cache but leaves Recorder statistics intact. Changing the
occupation or meter installation date can change which dates Ocea is asked for,
but does not change the PDS statistic ID.

Home Assistant does not support a fixed or entity price for an external water
statistic; this integration does not provide a separate cost statistic.

The resident portal identifies the thermal fluid as `Cetc` and maps it to
kWh. The confirmed daily PDS response contains dated `consommations` values
in kWh; dwelling responses use the same expected format. The integration
requires kWh for both and rejects a different or missing unit.
Unavailable or invalid data is not imported. An unavailable installation date
also prevents importing that PDS. Daily values must represent consumption
rather than cumulative meter indexes. Dwelling and PDS heating statistics may
overlap and should not be counted together. An energy statistic does not
establish that the heat is electricity or that a specific Energy dashboard
section is appropriate for it.

The former dwelling-level CETC sensor is no longer provided, even when no CETC
PDS history is available. External statistics are not sensor entities: update
any dashboards and automations that referenced the old sensor. Its existing
Recorder history is not deleted or migrated into the new statistics.

## Troubleshooting

### The integration cannot connect or asks for authentication

Check that you can sign in to the Ocea resident portal and that Home Assistant
can access the internet. If your credentials have changed, use the integration's
**Reconfigure** action to update them. If a meter is missing after replacement,
reload the integration to rediscover the available meters. The System Health
page can help identify connection or meter-reading failures.

Debug logging can also be enabled:

```yaml
logger:
  logs:
    custom_components.ocea_smart_building: debug
```

The integration's debug messages summarize API response shapes, types and
selected formats without logging account data or measurement values. Unknown
field names are replaced by temporary per-client fingerprints, so they cannot
be recovered from the logs. Other Home Assistant components may log different
information; review logs before sharing them.

## Removing the integration

In Settings → Devices & Services, open Ocea Smart Building and select
**Delete** from the integration menu. This removes its configuration and
entities from Home Assistant; it does not delete your Ocea account.
Then uninstall it from **Settings** → **Marketplace** (or from HACS on Home
Assistant 2026.10 and below) and restart Home Assistant to remove the
integration files.

<!-- Badge references -->
[marketplace-badge]: https://img.shields.io/badge/Home%20Assistant-Marketplace-41BDF5.svg?logo=home-assistant&logoColor=white
[marketplace-url]: https://my.home-assistant.io/redirect/marketplace_repository/?owner=Triskae&repository=ha_ocea_smart_building&category=integration
[release-badge]: https://img.shields.io/github/v/release/Triskae/ha_ocea_smart_building
[release-url]: https://github.com/Triskae/ha_ocea_smart_building/releases
[installations-badge]: https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.ocea_smart_building.total
[installations-url]: https://analytics.home-assistant.io/custom_integrations.json
