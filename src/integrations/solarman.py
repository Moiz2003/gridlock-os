"""
solarman.py — Inverex Local Telemetry Integration
Reads inverter telemetry directly over the local network through the
Solarman Wi-Fi dongle on port 8899 using pysolarmanv5.

Inverex units are rebranded Deye inverters, so we read standard Deye
holding registers for battery SoC, PV power, and load power.

Public API:
    get_telemetry() -> SolarTelemetry
        Returns SoC (%), PV yield (kW), and load (kW).
    get_battery_soc() -> float
        Convenience wrapper that returns only the SoC.
"""

import logging
import socket
from dataclasses import dataclass

from pysolarmanv5 import PySolarmanV5

import config

log = logging.getLogger("gridlock.solarman")

# Inverex Nitrox 8kW (Single-Phase Deye) Modbus holding registers
_REG_BATTERY_SOC = 184         # Battery SoC (%)
_REG_PV1_POWER_W = 186          # PV1 power (W)
_REG_PV2_POWER_W = 187          # PV2 power (W)
_REG_TOTAL_LOAD_POWER_W = 178   # Total load power (W)


@dataclass(frozen=True)
class SolarTelemetry:
    """Live inverter snapshot returned by get_telemetry()."""
    soc: float          # Battery state of charge, 0–100 %
    pv_yield_kw: float  # Present PV generation in kilowatts
    load_kw: float      # Present household consumption in kilowatts


def _read_s16(inverter: PySolarmanV5, register: int) -> int:
    """Reads one signed 16-bit holding register to prevent negative underflow."""
    values = inverter.read_holding_registers(register, 1)
    if not values:
        raise RuntimeError(f"Empty Modbus response for register {register}")
    value = int(values[0])
    return value if value < 32768 else value - 65536


def get_telemetry() -> SolarTelemetry:
    """Reads local Deye/Inverex telemetry via the Solarman Wi-Fi dongle."""
    inverter = PySolarmanV5(
        config.INVERTER_IP,
        config.INVERTER_SERIAL,
        port=config.INVERTER_PORT,
    )

    try:
        battery_soc = float(_read_s16(inverter, _REG_BATTERY_SOC))
        pv1_w = _read_s16(inverter, _REG_PV1_POWER_W)
        pv2_w = _read_s16(inverter, _REG_PV2_POWER_W)
        load_w = _read_s16(inverter, _REG_TOTAL_LOAD_POWER_W)

        telemetry = SolarTelemetry(
            soc=max(0.0, min(100.0, battery_soc)),
            pv_yield_kw=max(0.0, (pv1_w + pv2_w) / 1000.0),
            load_kw=max(0.0, load_w / 1000.0),
        )
        log.debug("Telemetry via local Modbus: %s", telemetry)
        return telemetry

    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(
            "Inverter telemetry timeout via local Modbus (dongle may be offline)."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Inverter telemetry socket error: {exc}") from exc
    finally:
        # Some versions expose close(), others disconnect(); support both safely.
        close_fn = getattr(inverter, "close", None) or getattr(inverter, "disconnect", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass


def get_battery_soc() -> float:
    """Convenience wrapper — returns only SoC. Prefer get_telemetry() for new callers."""
    return get_telemetry().soc
