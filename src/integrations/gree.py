"""
gree.py — Gree AC Local Network Integration
Sends commands to the Gree AC over the local Wi-Fi network using the
greeclimate library, which implements the Gree UDP protocol.

This module only sends commands. It does not make decisions.
"""

import logging
from asyncio import TimeoutError as AsyncTimeoutError
from asyncio import run as run_async
from asyncio import wait_for
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from greeclimate.device import Device, DeviceInfo, Mode, FanSpeed
from influxdb_client import InfluxDBClient

import config

log = logging.getLogger("gridlock.gree")

_LAST_GOOD_GREE_STATE: dict[str, Any] | None = None
_LAST_GOOD_GREE_TS: datetime | None = None
_LAST_GOOD_GREE_CACHE_FILE = Path("/tmp/gridlock_gree_last_state.json")
_GREE_CONNECT_FAILURES = 0
_GREE_NEXT_RETRY_AT: datetime | None = None
_GREE_LAST_PROBE_TS: datetime | None = None

_GREE_BACKOFF_BASE_SECONDS = 3
_GREE_BACKOFF_CAP_SECONDS = 60
_GREE_FRESH_PROBE_INTERVAL_SECONDS = 60
_GREE_OFFLINE_STALE_THRESHOLD_SECONDS = 300


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _next_backoff_seconds(failures: int) -> int:
    # 1,2,3 failures -> 3s, 6s, 12s (capped afterwards).
    return min(_GREE_BACKOFF_BASE_SECONDS * (2 ** max(0, failures - 1)), _GREE_BACKOFF_CAP_SECONDS)


def _write_last_good_cache(state: dict[str, Any], ts: datetime) -> None:
    payload = {
        "state": state,
        "timestamp": ts.isoformat(),
    }
    try:
        _LAST_GOOD_GREE_CACHE_FILE.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except Exception:
        # Cache persistence should never affect telemetry collection.
        pass


def _read_last_good_cache() -> tuple[dict[str, Any], datetime] | None:
    try:
        raw = _LAST_GOOD_GREE_CACHE_FILE.read_text(encoding="utf-8")
        payload = json.loads(raw)
        state = payload.get("state")
        ts_raw = payload.get("timestamp")
        if not isinstance(state, dict) or not isinstance(ts_raw, str):
            return None
        ts = datetime.fromisoformat(ts_raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return state, ts
    except Exception:
        return None


def _read_last_good_from_influx() -> tuple[dict[str, Any], datetime] | None:
    query = f'''
from(bucket: "{config.INFLUXDB_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "gridlock_snapshot")
  |> filter(fn: (r) => r._field == "ac_gree_state")
    |> filter(fn: (r) => r._value != "{{}}")
  |> last()
'''

    try:
        with InfluxDBClient(
            url=config.INFLUXDB_URL,
            token=config.INFLUXDB_TOKEN,
            org=config.INFLUXDB_ORG,
        ) as client:
            result = client.query_api().query(query=query)

        for table in result:
            for record in table.records:
                raw = record.get_value()
                if not isinstance(raw, str):
                    continue
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    continue
                if not parsed:
                    continue
                rec_time = record.get_time()
                if rec_time is None:
                    rec_time = datetime.now(tz=timezone.utc)
                return parsed, rec_time
    except Exception:
        return None

    return None

_MODE_MAP = {
    "cool": Mode.Cool,
    "heat": Mode.Heat,
    "auto": Mode.Auto,
    "fan":  Mode.Fan,
    "dry":  Mode.Dry,
}

_FAN_MAP = {
    "auto":   FanSpeed.Auto,
    "low":    FanSpeed.Low,
    "medium": FanSpeed.Medium,
    "high":   FanSpeed.High,
}

_FAN_SPEED_NAME = {
    int(FanSpeed.Auto): "auto",
    int(FanSpeed.Low): "low",
    int(FanSpeed.MediumLow): "medium_low",
    int(FanSpeed.Medium): "medium",
    int(FanSpeed.MediumHigh): "medium_high",
    int(FanSpeed.High): "high",
}


@dataclass
class GreeACState:
    power: bool
    target_temp: float
    current_temp: float
    fan_speed: str


class GreeListener:
    """Read-only listener for current Gree AC state over local network."""

    def __init__(self) -> None:
        if not config.GREE_AC_IP or not config.GREE_AC_MAC or not config.GREE_AC_KEY:
            raise RuntimeError("Gree listener is not configured (GREE_AC_IP/GREE_AC_MAC/GREE_AC_KEY)")

        self._device_info = DeviceInfo(
            ip=config.GREE_AC_IP,
            port=config.GREE_AC_PORT,
            mac=config.GREE_AC_MAC,
            name="GridLock-Gree",
        )

    def read_state(self) -> dict[str, Any]:
        async def _update_once() -> dict[str, Any]:
            device = Device(self._device_info)
            device.device_key = config.GREE_AC_KEY
            try:
                # Prevent a stalled UDP exchange from blocking and dropping the whole cycle.
                await wait_for(device.update_state(), timeout=3.5)
                raw_fan_speed = device.fan_speed
                state = GreeACState(
                    power=bool(device.power),
                    target_temp=float(device.target_temperature),
                    current_temp=float(device.current_temperature),
                    fan_speed=_FAN_SPEED_NAME.get(int(raw_fan_speed), str(raw_fan_speed)),
                )
                return asdict(state)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, AsyncTimeoutError):
                    raise RuntimeError("Gree listener read timed out") from exc
                raise RuntimeError(f"Gree listener read failed: {exc.__class__.__name__}: {exc}") from exc

        global _LAST_GOOD_GREE_STATE, _LAST_GOOD_GREE_TS
        global _GREE_CONNECT_FAILURES, _GREE_NEXT_RETRY_AT, _GREE_LAST_PROBE_TS

        now = _now_utc()
        next_retry_due = _GREE_NEXT_RETRY_AT is None or now >= _GREE_NEXT_RETRY_AT
        fresh_probe_due = _GREE_LAST_PROBE_TS is None or (
            (now - _GREE_LAST_PROBE_TS).total_seconds() >= _GREE_FRESH_PROBE_INTERVAL_SECONDS
        )
        should_probe_live = next_retry_due or fresh_probe_due

        if should_probe_live:
            _GREE_LAST_PROBE_TS = now
            try:
                # greeclimate's state API is async; run it synchronously inside the engine cycle.
                state = run_async(_update_once())
                _LAST_GOOD_GREE_STATE = state
                _LAST_GOOD_GREE_TS = _now_utc()
                _GREE_CONNECT_FAILURES = 0
                _GREE_NEXT_RETRY_AT = None
                _write_last_good_cache(_LAST_GOOD_GREE_STATE, _LAST_GOOD_GREE_TS)

                state["source"] = "live"
                state["stale_seconds"] = 0
                state["gree_connection_state"] = "live"
                state["gree_last_live_ts"] = _iso(_LAST_GOOD_GREE_TS)
                state["gree_connect_failures"] = _GREE_CONNECT_FAILURES
                return state
            except Exception as exc:
                _GREE_CONNECT_FAILURES += 1
                backoff_s = _next_backoff_seconds(_GREE_CONNECT_FAILURES)
                _GREE_NEXT_RETRY_AT = _now_utc() + timedelta(seconds=backoff_s)
                log.warning(
                    "Gree probe failed (failures=%d); next retry in %ss: %s",
                    _GREE_CONNECT_FAILURES,
                    backoff_s,
                    exc,
                )

        if _LAST_GOOD_GREE_STATE is None or _LAST_GOOD_GREE_TS is None:
            cached_from_disk = _read_last_good_cache()
            if cached_from_disk is not None:
                _LAST_GOOD_GREE_STATE, _LAST_GOOD_GREE_TS = cached_from_disk
            else:
                cached_from_influx = _read_last_good_from_influx()
                if cached_from_influx is not None:
                    _LAST_GOOD_GREE_STATE, _LAST_GOOD_GREE_TS = cached_from_influx
                    _write_last_good_cache(_LAST_GOOD_GREE_STATE, _LAST_GOOD_GREE_TS)

        if _LAST_GOOD_GREE_STATE is None or _LAST_GOOD_GREE_TS is None:
            return {
                "source": "offline",
                "stale_seconds": _GREE_OFFLINE_STALE_THRESHOLD_SECONDS,
                "gree_connection_state": "offline",
                "gree_last_live_ts": None,
                "gree_connect_failures": _GREE_CONNECT_FAILURES,
            }

        age_s = int((_now_utc() - _LAST_GOOD_GREE_TS).total_seconds())
        cached = dict(_LAST_GOOD_GREE_STATE)
        cached["source"] = "cached"
        cached["stale_seconds"] = age_s
        cached["gree_connection_state"] = (
            "offline" if age_s > _GREE_OFFLINE_STALE_THRESHOLD_SECONDS else "degraded"
        )
        cached["gree_last_live_ts"] = _iso(_LAST_GOOD_GREE_TS)
        cached["gree_connect_failures"] = _GREE_CONNECT_FAILURES
        if should_probe_live:
            log.warning("Gree listener transient failure; using cached state (%ss old)", age_s)
        else:
            log.debug("Gree backoff active; using cached state (%ss old)", age_s)
        return cached


def get_gree_state() -> dict[str, Any]:
    """Convenience wrapper used by the engine for passive telemetry reads."""
    return GreeListener().read_state()


def set_gree_ac(power: bool, temp_c: int = 24, mode: str = "cool", fan_speed: str = "auto") -> None:
    """
    Sends a state command to the Gree AC.

    Args:
        power:     True to turn ON, False for OFF.
        temp_c:    Target temperature in Celsius (16–30).
        mode:      Operating mode — 'cool', 'heat', 'auto', 'fan', 'dry'.
        fan_speed: Fan speed — 'auto', 'low', 'medium', 'high'.
    """
    device_info = DeviceInfo(
        ip=config.GREE_AC_IP,
        port=config.GREE_AC_PORT,
        mac=config.GREE_AC_MAC,
        name="GridLock-Gree",
    )

    try:
        device = Device(device_info)
        device.key = config.GREE_AC_KEY

        device.power = power
        if power:
            device.target_temperature = max(16, min(30, temp_c))
            device.mode = _MODE_MAP.get(mode, Mode.Cool)
            device.fan_speed = _FAN_MAP.get(fan_speed, FanSpeed.Auto)

        device.push_state_update()
        log.info("Gree AC command sent — power=%s, temp=%d°C, mode=%s, fan=%s", power, temp_c, mode, fan_speed)

    except Exception as exc:
        raise RuntimeError(f"Gree AC command failed: {exc}") from exc
