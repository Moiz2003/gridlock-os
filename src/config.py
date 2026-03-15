"""
config.py — Environment Configuration Loader
Reads and validates all variables from the .env file.
Every other module imports from here — nothing reads os.environ directly.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Raise a clear error at startup if a required variable is missing."""
    value = os.environ.get(key)
    if not value:
        raise EnvironmentError(
            f"[GridLock Config] Required environment variable '{key}' is not set. "
            "Check your .env file."
        )
    return value


def _optional(key: str, default: str) -> str:
    return os.environ.get(key, default)


# ── OpenWeatherMap ─────────────────────────────────────────────────────────────
OWM_API_KEY: str = _require("OWM_API_KEY")
OWM_CITY: str = _optional("OWM_CITY", "Lahore")
OWM_COUNTRY_CODE: str = _optional("OWM_COUNTRY_CODE", "PK")
OWM_LATITUDE: float = float(_optional("OWM_LATITUDE", "31.5497"))   # Lahore default
OWM_LONGITUDE: float = float(_optional("OWM_LONGITUDE", "74.3436"))  # Lahore default

# ── Inverex Local Modbus ───────────────────────────────────────────────────────
INVERTER_IP: str = _require("INVERTER_IP")
INVERTER_PORT: int = int(_optional("INVERTER_PORT", "8899"))
INVERTER_SERIAL: int = int(_require("INVERTER_SERIAL"))

# ── Gree AC ────────────────────────────────────────────────────────────────────
GREE_AC_IP: str = _require("GREE_AC_IP")
GREE_AC_PORT: int = int(_optional("GREE_AC_PORT", "7000"))
GREE_AC_MAC: str = _require("GREE_AC_MAC")
GREE_AC_KEY: str = _require("GREE_AC_KEY")

# ── Panasonic Comfort Cloud ────────────────────────────────────────────────────
PANASONIC_USERNAME: str = _require("PANASONIC_USERNAME")
PANASONIC_PASSWORD: str = _require("PANASONIC_PASSWORD")
PANASONIC_DEVICE_GUID: str = _require("PANASONIC_DEVICE_GUID")

# ── InfluxDB ───────────────────────────────────────────────────────────────────
INFLUXDB_URL: str = _optional("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN: str = _require("INFLUXDB_TOKEN")
INFLUXDB_ORG: str = _optional("INFLUXDB_ORG", "gridlock")
INFLUXDB_BUCKET: str = _optional("INFLUXDB_BUCKET", "home_energy")

# ── Engine Tuning ──────────────────────────────────────────────────────────────
BATTERY_LOW_THRESHOLD: int = int(_optional("BATTERY_LOW_THRESHOLD", "30"))
BATTERY_SUNSET_TARGET: int = int(_optional("BATTERY_SUNSET_TARGET", "80"))
PRIME_DIRECTIVE_HOUR: int = int(_optional("PRIME_DIRECTIVE_HOUR", "17"))# Predicted SoC @ 17:00 below this triggers the battery-save directive
PRIME_DIRECTIVE_SOC_TARGET: int = int(_optional("PRIME_DIRECTIVE_SOC_TARGET", "95"))

# ── Battery / Predictor ──────────────────────────────────────────────
BATTERY_CAPACITY_KWH: float = float(_optional("BATTERY_CAPACITY_KWH", "10.24"))
# Absolute path to a joblib-serialised sklearn/XGBoost model; leave blank to use heuristic
MODEL_PATH: str = _optional("MODEL_PATH", "")

# ── Safety ───────────────────────────────────────────────────────
DRY_RUN: bool = _optional("DRY_RUN", "false").lower() in ("true", "1", "yes")