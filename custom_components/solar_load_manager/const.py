"""Constants for the Solar Load Manager integration."""

DOMAIN = "solar_load_manager"

# Hub config keys
CONF_SURPLUS_SENSOR = "surplus_sensor"
CONF_PRICE_SENSOR = "price_sensor"
CONF_PRICE_MIN_SENSOR = "price_min_sensor"
CONF_PRICE_MAX_SENSOR = "price_max_sensor"
CONF_SMOOTHING_SECONDS = "smoothing_seconds"
CONF_IMPORT_TOLERANCE = "import_tolerance"
CONF_CHEAP_SCORE = "cheap_score"
CONF_OVERRIDE_MINUTES = "override_minutes"

# Device config keys
CONF_DEVICES = "devices"
CONF_NAME = "name"
CONF_PRIORITY = "priority"
CONF_DEVICE_TYPE = "device_type"
CONF_ENTITY = "entity"
CONF_RATED_POWER = "rated_power"
CONF_ON_FACTOR = "on_factor"
CONF_MIN_ON = "min_on_minutes"
CONF_MIN_OFF = "min_off_minutes"
CONF_BLOCK_SCORE = "block_score"
CONF_HVAC_MODE = "hvac_mode"
CONF_MUST_RUN_ENABLED = "must_run_enabled"
CONF_MUST_RUN_START = "must_run_start"
CONF_MUST_RUN_END = "must_run_end"
# Tesla-specific
CONF_CHARGE_SWITCH = "charge_switch"
CONF_CURRENT_NUMBER = "current_number"
CONF_CABLE_SENSOR = "cable_sensor"
CONF_CHARGER_POWER_SENSOR = "charger_power_sensor"
CONF_PHASES = "phases"
CONF_VOLTAGE = "voltage"
CONF_MIN_AMPS = "min_amps"
CONF_MAX_AMPS = "max_amps"

DEVICE_TYPE_SWITCH = "switch"
DEVICE_TYPE_CLIMATE = "climate"
DEVICE_TYPE_TESLA = "tesla"
DEVICE_TYPES = [DEVICE_TYPE_SWITCH, DEVICE_TYPE_CLIMATE, DEVICE_TYPE_TESLA]

DEFAULT_SMOOTHING_SECONDS = 300
DEFAULT_IMPORT_TOLERANCE = 300
DEFAULT_CHEAP_SCORE = 0.1
DEFAULT_OVERRIDE_MINUTES = 30
DEFAULT_ON_FACTOR = 1.1
DEFAULT_MIN_ON = 15
DEFAULT_MIN_OFF = 10
DEFAULT_BLOCK_SCORE = 1.01
DEFAULT_VOLTAGE = 230
DEFAULT_PHASES = 3
DEFAULT_MIN_AMPS = 5
DEFAULT_MAX_AMPS = 16

UPDATE_INTERVAL_SECONDS = 60

SERVICE_BOOST = "boost"
ATTR_DEVICE = "device"
ATTR_MINUTES = "minutes"

# Device decision states
STATE_DISABLED = "disabled"
STATE_IDLE = "idle"
STATE_SURPLUS = "running_surplus"
STATE_CHEAP = "running_cheap"
STATE_MUST_RUN = "must_run"
STATE_BOOST = "boost"
STATE_SHED = "shed"
STATE_OVERRIDE = "manual_override"
STATE_ANTI_CYCLE = "anti_cycle_wait"
STATE_UNAVAILABLE = "unavailable"
