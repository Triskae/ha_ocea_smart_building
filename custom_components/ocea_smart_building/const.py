"""Constants for the Ocea Smart Building integration."""

DOMAIN = "ocea_smart_building"
INTEGRATION_VERSION = "1.2.0"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36 "
    f"Home-Assistant-Ocea-Smart-Building/{INTEGRATION_VERSION}"
)

# Config keys
CONF_LOCAL_ID = "local_id"

# Sentinel used to avoid exposing the stored password in config flow forms
PASSWORD_NOT_CHANGED = "__**password_not_changed**__"

# Defaults
DEFAULT_SCAN_INTERVAL = 18000  # 5 hours

# Maps an Ocea fluid code to its dwelling-level sensor key.
WATER_FLUID_SENSOR_KEYS: dict[str, str] = {
    "EauFroide": "eau_froide",
    "EauChaude": "eau_chaude",
}
