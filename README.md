# Ocea Smart Building - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant integration to monitor cold and hot water consumption from [Ocea Smart Building](https://espace-resident.ocea-sb.com) resident portal.

## Features

- **Cold water** consumption in m³
- **Hot water** consumption in m³
- Compatible with the **Energy dashboard** (water section)
- Automatic Azure AD B2C authentication (no headless browser needed)
- Automatic token refresh
- UI-based configuration

## Installation

### Via HACS (recommended)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add the repository URL, category **Integration**
3. Search for "Ocea Smart Building" → Install
4. Restart Home Assistant

### Manual

Copy `custom_components/ocea_smart_building/` into `config/custom_components/` and restart Home Assistant.

## Configuration

Settings → Devices & Services → Add Integration → "Ocea Smart Building"

- **Email**: your Ocea resident portal email
- **Password**: your Ocea password

Your dwelling is automatically detected from your Ocea account.

## Energy dashboard

The sensors can be added directly in Settings → Dashboards → Energy → Water consumption.

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.ocea_smart_building: debug
```
