# Ocea Smart Building - Home Assistant Integration

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
![downloads][downloads-badge]

Home Assistant integration to monitor water and heating consumption from the [Ocea Smart Building](https://espace-resident.ocea-sb.com) resident portal.

## Features

- **Cold water** consumption in m³
- **Hot water** consumption in m³
- **CETC heating** consumption in kWh when available for the dwelling
- Individual water-meter consumption when the Ocea account exposes several PDS
  (points of measurement)
- Leak detection for each individual water meter
- Compatible with the **Energy dashboard** (water and energy sections)
- Automatic Azure AD B2C authentication (no headless browser needed)
- Automatic token refresh
- UI-based configuration
- Native Home Assistant reconfiguration for the email and password

## Installation

### Via HACS (recommended)

This integration is available in the official HACS default repository.

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Triskae&repository=ha_ocea_smart_building&category=integration)

1. In Home Assistant, open **HACS** → **Integrations**
2. Search for **Ocea Smart Building**
3. Select the integration and click **Download**
4. Restart Home Assistant

There is no need to add this repository as a custom repository.

### Manual

Copy `custom_components/ocea_smart_building/` into `config/custom_components/` and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and add the integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ocea_smart_building)

Settings → Devices & Services → Add Integration → "Ocea Smart Building"

- **Email**: your Ocea resident portal email
- **Password**: your Ocea password

If your account has one accessible dwelling, it is selected automatically. If
several are accessible, choose the dwelling to monitor. The displayed occupation
status is indicative, based on the dates supplied by Ocea, and does not
determine access.

You can add multiple Ocea accounts as separate Home Assistant entries, but the
same email address can only be configured once. The integration's System Health
page aggregates update status and per-meter reading availability across all
loaded entries. After a failed update, cached readings for that account are
counted as unavailable until its next successful update.

### Water meters

Ocea exposes two levels of consumption data:

- the standard cold- and hot-water sensors are totals for the dwelling;
- individual meter sensors are created for each cold- or hot-water PDS.

Individual sensors use the PDS identifier as their stable Home Assistant
identity. When Ocea provides a serial number, it is shown in the sensor and
device name alongside the PDS identifier. This keeps sensors distinguishable
when several meters measure the same fluid.

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

If Ocea returns an invalid or incomplete dated reading, the integration skips
it and logs a warning. A meter's resulting total may be incomplete and may
affect Home Assistant's consumption statistics. If every returned reading is
invalid, that meter's consumption is unavailable instead of reported as zero.

For example, the individual meter sensors can help compare consumption between
rooms, while the dwelling totals can be tracked in the Energy dashboard.

The integration discovers the available PDS and meter serial numbers during
setup, then refreshes each meter's consumption for the current calendar month.
If one meter is temporarily unavailable, its entity remains present but has no
state until a later refresh succeeds; other meters and dwelling totals continue
to update. The discovery list is cached while the integration is loaded. Reload
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

The CLI displays dashboard totals and queries every measurement point (PDS)
of the first dwelling, including heating and other fluids. By default it
requests daily granularity from the start of the current month through today.
To choose an inclusive interval, add both `--start-date YYYY-MM-DD` and
`--end-date YYYY-MM-DD`. The default dates use the machine's local calendar;
the API request expresses each bound at the start or end of the day in UTC.
`--verbose` adds sanitized request traces.

The API may return no readings or readings spaced by months even when `Day` is
requested; the CLI displays only what Ocea actually returns. Daily readings
are available the following day, so today and future dates are excluded when
checking for missing readings. A failed PDS is reported without stopping the
others, and causes a nonzero exit status.
This tool does not import historical data into Home Assistant.

Dates and consumption values are private even when PDS identifiers are omitted.
Review and anonymize output before sharing it. Do not share token output:
`--dump-tokens` deliberately displays parts of the authentication tokens.

### Reconfiguration

Open Settings → Devices & Services → Ocea Smart Building, then select the native **Reconfigure** action.

- The current email address is prefilled and can be changed.
- The stored password is never displayed. Leave the password field unchanged to keep it, or enter a new password to replace it.
- Credentials are validated with Ocea before changes are saved.
- Home Assistant reloads the integration only when the validated configuration has actually changed.

If Home Assistant requests reauthentication, sign in with the same Ocea
account. To switch accounts, use **Reconfigure** instead. A successful
reauthentication also refreshes the entry name from the account. Both
reauthentication and reconfiguration retain the selected dwelling. If it is
missing or no longer accessible, restore access with Ocea and retry.

To select a different dwelling, remove the integration entry and install it
again. Removing it also removes its devices and entities: automations, dashboards
and other references may need updating, and historical data may no longer be
associated with the new entities. Review these dependencies before removal.

## Energy dashboard

Add the cold and hot water sensors in Settings → Dashboards → Energy → Water consumption. When available, the CETC heating sensor can also be added as an energy-consumption source.

Individual water-meter sensors are useful for separate dashboards and
comparisons. Use the dwelling-level sensors when you need the total reported by
the Ocea resident portal. Do not add both dwelling totals and their individual
meters to the same Energy Dashboard water configuration, or the same water
consumption will be counted twice.

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
If installed through HACS, uninstall it there as well and restart Home Assistant
to remove the integration files.

<!-- Badge references -->
[hacs-badge]: https://img.shields.io/badge/HACS-Default-41BDF5.svg
[hacs-url]: https://github.com/hacs/default
[release-badge]: https://img.shields.io/github/v/release/Triskae/ha_ocea_smart_building
[release-url]: https://github.com/Triskae/ha_ocea_smart_building/releases
[downloads-badge]: https://img.shields.io/github/downloads/Triskae/ha_ocea_smart_building/total
