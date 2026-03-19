"""
time_series.py — InfluxDB Writer
Pushes each engine cycle's telemetry snapshot into the GridLock bucket.
All other modules are completely unaware of InfluxDB — this is the only file
that touches the database.
"""

import logging
import json
from datetime import datetime, timezone
from time import sleep
from typing import Any

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import config

log = logging.getLogger("gridlock.db")


def _safe_bool_as_int(state: dict[str, Any], key: str) -> int | None:
    value = state.get(key)
    if isinstance(value, bool):
        return int(value)
    return None


def _safe_float(state: dict[str, Any], key: str) -> float | None:
    value = state.get(key)
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    if value is None:
        return None
    return str(value)


def _safe_int(state: dict[str, Any], key: str) -> int | None:
    value = state.get(key)
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def write_snapshot(
    battery_soc: float,
    cloud_cover: int,
    outside_temp_c: float,
    ac_power: bool,
    ac_temp_setpoint: int,
    predicted_soc_at_1700: float = 0.0,
    pv_yield_kw: float = 0.0,
    load_kw: float = 0.0,
    ac_output_power_kw: float | None = None,
    daily_pv_energy_kwh: float | None = None,
    daily_load_energy_kwh: float | None = None,
    total_energy_kwh: float | None = None,
    inverter_efficiency: float | None = None,
    theoretical_pv_potential: float = 0.0,
    is_clipping: bool = False,
    solar_health_score: float = 1.0,
    forecast_max_temp_3d: float = 0.0,
    ac_gree_state: dict[str, Any] | None = None,
    ac_panasonic_state: dict[str, Any] | None = None,
) -> None:
    """
    Writes a single time-series data point to InfluxDB.

    Fields:
        battery_soc            — Battery state of charge (%)
        cloud_cover            — Cloud cover from OWM (%)
        outside_temp_c         — Current outside temperature (°C)
        ac_power               — Whether both ACs are on (bool → int 0/1)
        ac_temp_setpoint       — Temperature setpoint sent to the ACs (°C)
        predicted_soc_at_1700  — Model/heuristic SoC prediction for 17:00 (%)
        pv_yield_kw            — Real-time PV generation (kW)
        load_kw                — Real-time house load (kW)
        ac_output_power_kw     — Inverter AC-side instantaneous output power (kW)
        daily_pv_energy_kwh    — Inverter daily PV energy counter (kWh)
        daily_load_energy_kwh  — Inverter daily load energy counter (kWh)
        total_energy_kwh       — Inverter lifetime energy counter (kWh)
        inverter_efficiency    — Instantaneous AC/DC ratio proxy
        theoretical_pv_potential — PV potential estimate from cloud dampening (kW)
        is_clipping            — Whether clipping condition is currently active
        solar_health_score     — Real/expected PV ratio proxy (0+)
        forecast_max_temp_3d   — Max forecast temp over the next 3 days (°C)
        ac_gree_state          — Read-only Gree AC state snapshot as JSON string
        ac_panasonic_state     — Read-only Panasonic AC state snapshot as JSON string
        gree_power             — Flattened Gree power state (0/1)
        gree_temp_target       — Flattened Gree target temperature (°C)
        gree_temp_actual       — Flattened Gree measured indoor temperature (°C)
        gree_fan_speed         — Flattened Gree fan speed as text
        panasonic_power        — Flattened Panasonic power state (0/1)
        panasonic_temp_target  — Flattened Panasonic target temperature (°C)
        panasonic_temp_actual  — Flattened Panasonic measured indoor temperature (°C)
        panasonic_fan_speed    — Flattened Panasonic fan speed as text
        gree_state_fresh       — 1 when Gree state is live, 0 when cached/offline
        gree_stale_seconds     — Age of last known live Gree state in seconds
        gree_connect_failures  — Consecutive failed Gree live probes
        gree_stale             — Data quality flag; 1 means stale/low-trust Gree sample
    """

    gree_state = ac_gree_state or {}
    panasonic_state = ac_panasonic_state or {}

    gree_state_payload = json.dumps(gree_state, sort_keys=True)
    panasonic_state_payload = json.dumps(panasonic_state, sort_keys=True)

    point = (
        Point("gridlock_snapshot")
        .tag("location", "home")
        .field("battery_soc", battery_soc)
        .field("cloud_cover", cloud_cover)
        .field("outside_temp_c", outside_temp_c)
        .field("ac_power", int(ac_power))
        .field("ac_temp_setpoint", ac_temp_setpoint)
        .field("predicted_soc_at_1700", predicted_soc_at_1700)
        .field("pv_yield_kw", pv_yield_kw)
        .field("load_kw", load_kw)
        .field("theoretical_pv_potential", theoretical_pv_potential)
        .field("is_clipping", int(is_clipping))
        .field("solar_health_score", solar_health_score)
        .field("forecast_max_temp_3d", forecast_max_temp_3d)
        .field("ac_gree_state", gree_state_payload)
        .field("ac_panasonic_state", panasonic_state_payload)
        .time(datetime.now(tz=timezone.utc), WritePrecision.S)
    )

    if ac_output_power_kw is not None:
        point.field("ac_output_power_kw", ac_output_power_kw)
    if daily_pv_energy_kwh is not None:
        point.field("daily_pv_energy_kwh", daily_pv_energy_kwh)
    if daily_load_energy_kwh is not None:
        point.field("daily_load_energy_kwh", daily_load_energy_kwh)
    if total_energy_kwh is not None:
        point.field("total_energy_kwh", total_energy_kwh)
    if inverter_efficiency is not None:
        point.field("inverter_efficiency", inverter_efficiency)

    # Flatten passive AC state into native fields for TSDB analytics and ML features.
    gree_power = _safe_bool_as_int(gree_state, "power")
    gree_temp_target = _safe_float(gree_state, "target_temp")
    gree_temp_actual = _safe_float(gree_state, "current_temp")
    gree_fan_speed = _safe_str(gree_state, "fan_speed")
    gree_stale_seconds = _safe_float(gree_state, "stale_seconds")
    gree_connect_failures = _safe_int(gree_state, "gree_connect_failures")

    gree_source = _safe_str(gree_state, "source")
    gree_state_fresh: int | None = None
    if gree_source is not None:
        gree_state_fresh = 1 if gree_source == "live" else 0

    gree_stale = False
    if gree_state_fresh == 0:
        gree_stale = True
    if gree_stale_seconds is not None and gree_stale_seconds > 300:
        gree_stale = True

    panasonic_power = _safe_bool_as_int(panasonic_state, "power")
    panasonic_temp_target = _safe_float(panasonic_state, "target_temp")
    panasonic_temp_actual = _safe_float(panasonic_state, "current_temp")
    panasonic_fan_speed = _safe_str(panasonic_state, "fan_speed")

    # If sample is stale, keep metadata but skip flattened Gree feature values.
    if not gree_stale:
        if gree_power is not None:
            point.field("gree_power", gree_power)
        if gree_temp_target is not None:
            point.field("gree_temp_target", gree_temp_target)
        if gree_temp_actual is not None:
            point.field("gree_temp_actual", gree_temp_actual)
        if gree_fan_speed is not None:
            point.field("gree_fan_speed", gree_fan_speed)
    if gree_state_fresh is not None:
        point.field("gree_state_fresh", gree_state_fresh)
    if gree_stale_seconds is not None:
        point.field("gree_stale_seconds", gree_stale_seconds)
    if gree_connect_failures is not None:
        point.field("gree_connect_failures", gree_connect_failures)
    point.field("gree_stale", int(gree_stale))

    if panasonic_power is not None:
        point.field("panasonic_power", panasonic_power)
    if panasonic_temp_target is not None:
        point.field("panasonic_temp_target", panasonic_temp_target)
    if panasonic_temp_actual is not None:
        point.field("panasonic_temp_actual", panasonic_temp_actual)
    if panasonic_fan_speed is not None:
        point.field("panasonic_fan_speed", panasonic_fan_speed)

    for attempt in range(1, 4):
        try:
            with InfluxDBClient(
                url=config.INFLUXDB_URL,
                token=config.INFLUXDB_TOKEN,
                org=config.INFLUXDB_ORG,
            ) as client:
                write_api = client.write_api(write_options=SYNCHRONOUS)
                write_api.write(bucket=config.INFLUXDB_BUCKET, record=point)
                log.debug("Snapshot written to InfluxDB.")
                return
        except Exception as exc:
            if attempt < 3:
                log.warning("Influx write failed (attempt %d/3): %s", attempt, exc)
                sleep(1)
            else:
                # Database unavailability should not crash the engine cycle
                log.error("Failed to write snapshot to InfluxDB after 3 attempts: %s", exc)
