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
from time import monotonic, sleep

from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5

import config

log = logging.getLogger("gridlock.solarman")

# Inverex Nitrox 8kW (Single-Phase Deye) Modbus holding registers
_REG_BATTERY_SOC = 184         # Battery SoC (%)
_REG_PV1_POWER_W = 186          # PV1 power (W)
_REG_PV2_POWER_W = 187          # PV2 power (W)
_REG_TOTAL_LOAD_POWER_W = 178   # Total load power (W)

_NETWORK_PARTITION_BACKOFF_SECONDS = 30
_NEXT_RECONNECT_AT_MONOTONIC: float | None = None


@dataclass(frozen=True)
class SolarTelemetry:
    """Live inverter snapshot returned by get_telemetry()."""
    soc: float          # Battery state of charge, 0–100 %
    pv_yield_kw: float  # Present PV generation in kilowatts
    load_kw: float      # Present household consumption in kilowatts
    ac_output_power_kw: float | None = None
    daily_pv_energy_kwh: float | None = None
    daily_load_energy_kwh: float | None = None
    total_energy_kwh: float | None = None
    inverter_efficiency: float | None = None


def _zero_telemetry() -> SolarTelemetry:
    return SolarTelemetry(soc=0.0, pv_yield_kw=0.0, load_kw=0.0)


def _is_network_partition_error(exc: Exception) -> bool:
    if isinstance(exc, NoSocketAvailableError):
        return True
    if isinstance(exc, OSError) and exc.errno == 101:
        return True
    return False


def _read_s16(inverter: PySolarmanV5, register: int) -> int:
    """Reads one signed 16-bit holding register to prevent negative underflow."""
    values = inverter.read_holding_registers(register, 1)
    if not values:
        raise RuntimeError(f"Empty Modbus response for register {register}")
    value = int(values[0])
    return value if value < 32768 else value - 65536


def _read_u16_optional(inverter: PySolarmanV5, register: int | None, scale: float = 1.0) -> float | None:
    if register is None:
        return None
    try:
        values = inverter.read_holding_registers(register, 1)
        if not values:
            return None
        raw = int(values[0])
        return max(0.0, raw * scale)
    except Exception as exc:
        log.debug("Optional register read failed (reg=%s): %s", register, exc)
        return None


def get_telemetry() -> SolarTelemetry:
    """Reads local Deye/Inverex telemetry via the Solarman Wi-Fi dongle."""
    global _NEXT_RECONNECT_AT_MONOTONIC

    now_monotonic = monotonic()
    if _NEXT_RECONNECT_AT_MONOTONIC is not None and now_monotonic < _NEXT_RECONNECT_AT_MONOTONIC:
        wait_s = max(1, int(_NEXT_RECONNECT_AT_MONOTONIC - now_monotonic))
        log.warning("[INVERTER_BACKOFF] Network partition backoff active; reconnect retry in ~%ss", wait_s)
        return _zero_telemetry()

    for attempt in range(1, 4):
        inverter = None
        try:
            inverter = PySolarmanV5(
                config.INVERTER_IP,
                config.INVERTER_SERIAL,
                port=config.INVERTER_PORT,
                auto_reconnect=True,
                socket_timeout=15,
            )

            battery_soc = float(_read_s16(inverter, _REG_BATTERY_SOC))
            pv1_w = _read_s16(inverter, _REG_PV1_POWER_W)
            pv2_w = _read_s16(inverter, _REG_PV2_POWER_W)
            load_w = _read_s16(inverter, _REG_TOTAL_LOAD_POWER_W)

            telemetry = SolarTelemetry(
                soc=max(0.0, min(100.0, battery_soc)),
                pv_yield_kw=max(0.0, (pv1_w + pv2_w) / 1000.0),
                load_kw=max(0.0, load_w / 1000.0),
            )

            ac_output_power_w = _read_u16_optional(
                inverter,
                config.INVERTER_REG_AC_OUTPUT_POWER_W,
                config.INVERTER_AC_OUTPUT_POWER_SCALE,
            )
            ac_output_power_kw = (ac_output_power_w / 1000.0) if ac_output_power_w is not None else None

            daily_pv_energy_kwh = _read_u16_optional(
                inverter,
                config.INVERTER_REG_DAILY_PV_ENERGY_KWH,
                config.INVERTER_DAILY_PV_ENERGY_SCALE,
            )
            daily_load_energy_kwh = _read_u16_optional(
                inverter,
                config.INVERTER_REG_DAILY_LOAD_ENERGY_KWH,
                config.INVERTER_DAILY_LOAD_ENERGY_SCALE,
            )
            total_energy_kwh = _read_u16_optional(
                inverter,
                config.INVERTER_REG_TOTAL_ENERGY_KWH,
                config.INVERTER_TOTAL_ENERGY_SCALE,
            )

            inverter_efficiency = None
            if ac_output_power_kw is not None and telemetry.pv_yield_kw > 0:
                inverter_efficiency = max(0.0, ac_output_power_kw / telemetry.pv_yield_kw)

            telemetry = SolarTelemetry(
                soc=telemetry.soc,
                pv_yield_kw=telemetry.pv_yield_kw,
                load_kw=telemetry.load_kw,
                ac_output_power_kw=ac_output_power_kw,
                daily_pv_energy_kwh=daily_pv_energy_kwh,
                daily_load_energy_kwh=daily_load_energy_kwh,
                total_energy_kwh=total_energy_kwh,
                inverter_efficiency=inverter_efficiency,
            )
            _NEXT_RECONNECT_AT_MONOTONIC = None
            log.debug("Telemetry via local Modbus: %s", telemetry)
            return telemetry

        except (NoSocketAvailableError, TimeoutError, socket.timeout, OSError) as exc:
            if _is_network_partition_error(exc):
                _NEXT_RECONNECT_AT_MONOTONIC = monotonic() + _NETWORK_PARTITION_BACKOFF_SECONDS
                if attempt < 3:
                    log.warning(
                        "Inverter network partition detected (attempt %d/3): %s. "
                        "Waiting %ss before re-initializing client.",
                        attempt,
                        exc,
                        _NETWORK_PARTITION_BACKOFF_SECONDS,
                    )
                    sleep(_NETWORK_PARTITION_BACKOFF_SECONDS)
                    continue
                log.warning("[INVERTER_OFFLINE] Network partition persists after retries: %s", exc)
                break

            if attempt < 3:
                log.warning("Inverter telemetry read failed (attempt %d/3): %s", attempt, exc)
                sleep(2)
            else:
                log.warning("[INVERTER_OFFLINE] Returning zeroed telemetry after 3 failed attempts: %s", exc)
        finally:
            # Some versions expose close(), others disconnect(); support both safely.
            close_fn = getattr(inverter, "close", None) or getattr(inverter, "disconnect", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    return _zero_telemetry()


def get_battery_soc() -> float:
    """Convenience wrapper — returns only SoC. Prefer get_telemetry() for new callers."""
    return get_telemetry().soc
