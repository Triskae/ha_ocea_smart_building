"""Constants for the OCEA Smart Building integration."""

from dataclasses import dataclass

from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

DOMAIN = "ocea_smart_building"
INTEGRATION_VERSION = "1.2.1"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36 "
    f"Home-Assistant-OCEA-Smart-Building/{INTEGRATION_VERSION}"
)

# Config keys
CONF_LOCAL_ID = "local_id"
CETC_FLUID = "Cetc"

# Sentinel used to avoid exposing the stored password in config flow forms
PASSWORD_NOT_CHANGED = "__**password_not_changed**__"

# Defaults
DEFAULT_SCAN_INTERVAL = 18000  # 5 hours

@dataclass(frozen=True, slots=True)
class PdsFluidMetadata:
    """Configuration derived from one supported OCEA fluid code."""

    api_unit: str
    dashboard_sensor_key: str
    fluid_key: str
    statistic_display_label: str
    statistic_unit_class: str
    statistic_unit_of_measurement: str
    supports_water_leak_sensors: bool


# Add fluids only when their API unit and Home Assistant statistics metadata
# have been verified.
PDS_FLUID_METADATA: dict[str, PdsFluidMetadata] = {
    "EauFroide": PdsFluidMetadata(
        api_unit="m3",
        dashboard_sensor_key="eau_froide",
        fluid_key="cold_water",
        statistic_display_label="Cold water",
        statistic_unit_class=VolumeConverter.UNIT_CLASS,
        statistic_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        supports_water_leak_sensors=True,
    ),
    "EauChaude": PdsFluidMetadata(
        api_unit="m3",
        dashboard_sensor_key="eau_chaude",
        fluid_key="hot_water",
        statistic_display_label="Hot water",
        statistic_unit_class=VolumeConverter.UNIT_CLASS,
        statistic_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        supports_water_leak_sensors=True,
    ),
    CETC_FLUID: PdsFluidMetadata(
        api_unit="kWh",
        dashboard_sensor_key="cetc",
        fluid_key="heating_energy",
        statistic_display_label="Heating energy",
        statistic_unit_class=EnergyConverter.UNIT_CLASS,
        statistic_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        supports_water_leak_sensors=False,
    ),
}
