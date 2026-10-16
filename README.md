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

[![Open your Home Assistant instance and open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Triskae&repository=ha_ocea_smart_building&category=integration)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add the repository URL, category **Integration**
3. Search for "Ocea Smart Building" → Install
4. Restart Home Assistant

### Manual

Copy `custom_components/ocea_smart_building/` into `config/custom_components/` and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and add the integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ocea_smart_building)

Settings → Devices & Services → Add Integration → "Ocea Smart Building"

- **Email**: your Ocea resident portal email
- **Password**: your Ocea password

Your dwelling is automatically detected from your Ocea account.

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

The integration discovers the available PDS and meter serial numbers during
setup, then refreshes each meter's consumption for the current calendar month.
If one meter is temporarily unavailable, its entity remains present but has no
state until a later refresh succeeds; other meters and dwelling totals continue
to update. The discovery list is cached while the integration is loaded. Reload
the integration after a meter is replaced, added, or removed in the Ocea portal
so Home Assistant can synchronize the meter entities.

### Reconfiguration

Open Settings → Devices & Services → Ocea Smart Building, then select the native **Reconfigure** action.

- The current email address is prefilled and can be changed.
- The stored password is never displayed. Leave the password field unchanged to keep it, or enter a new password to replace it.
- Credentials are validated with Ocea before changes are saved.
- Home Assistant reloads the integration only when the validated configuration has actually changed.

## Energy dashboard

Add the cold and hot water sensors in Settings → Dashboards → Energy → Water consumption. When available, the CETC heating sensor can also be added as an energy-consumption source.

Individual water-meter sensors are useful for separate dashboards and
comparisons. Use the dwelling-level sensors when you need the total reported by
the Ocea resident portal. Do not add both dwelling totals and their individual
meters to the same Energy Dashboard water configuration, or the same water
consumption will be counted twice.

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.ocea_smart_building: debug
```

<!-- Badge references -->
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/Triskae/ha_ocea_smart_building
[release-url]: https://github.com/Triskae/ha_ocea_smart_building/releases
[downloads-badge]: https://img.shields.io/github/downloads/Triskae/ha_ocea_smart_building/total
