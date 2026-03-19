"""
panasonic.py — Panasonic Comfort Cloud Integration
Sends commands to the Panasonic AC via the Comfort Cloud cloud API
using the pcomfortcloud library.

This module only sends commands. It does not make decisions.
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any

import pcomfortcloud

import config

log = logging.getLogger("gridlock.panasonic")

_MODE_MAP = {
    "cool": pcomfortcloud.constants.OperationMode.Cool,
    "heat": pcomfortcloud.constants.OperationMode.Heat,
    "auto": pcomfortcloud.constants.OperationMode.Auto,
    "fan":  pcomfortcloud.constants.OperationMode.Fan,
    "dry":  pcomfortcloud.constants.OperationMode.Dry,
}


@dataclass
class PanasonicACState:
    power: bool
    target_temp: float
    current_temp: float
    fan_speed: str


class PanasonicListener:
    """Read-only listener for Panasonic state via Comfort Cloud."""

    def __init__(self) -> None:
        if not config.PANASONIC_USERNAME or not config.PANASONIC_PASSWORD or not config.PANASONIC_DEVICE_GUID:
            raise RuntimeError(
                "Panasonic listener is not configured "
                "(PANASONIC_USERNAME/PANASONIC_PASSWORD/PANASONIC_DEVICE_GUID)"
            )

    def _resolve_device_id(self, session: pcomfortcloud.Session) -> str:
        configured_id = config.PANASONIC_DEVICE_GUID
        devices = session.get_devices()

        if any(device.get("id") == configured_id for device in devices):
            return configured_id

        # Accept either hashed id or raw device GUID in config.
        for hashed_id, raw_guid in session._deviceIndexer.items():  # noqa: SLF001
            if raw_guid == configured_id:
                return hashed_id

        raise RuntimeError("Configured Panasonic device id/guid was not found in Comfort Cloud account")

    def read_state(self) -> dict[str, Any]:
        session = pcomfortcloud.Session(
            username=config.PANASONIC_USERNAME,
            password=config.PANASONIC_PASSWORD,
        )

        try:
            session.login()
            device_id = self._resolve_device_id(session)
            device = session.get_device(device_id)
            if not device:
                raise RuntimeError("Comfort Cloud returned no device state")

            parameters = device.get("parameters", {})
            power = parameters.get("power")
            fan_speed = parameters.get("fanSpeed")

            state = PanasonicACState(
                power=bool(power == pcomfortcloud.constants.Power.On),
                target_temp=float(parameters.get("temperature", 0.0)),
                current_temp=float(parameters.get("temperatureInside", 0.0)),
                fan_speed=(fan_speed.name.lower() if fan_speed is not None else "unknown"),
            )
            return asdict(state)

        except Exception as exc:
            raise RuntimeError(f"Panasonic listener read failed: {exc}") from exc

        finally:
            try:
                session.logout()
            except Exception:
                pass


def get_panasonic_state() -> dict[str, Any]:
    """Convenience wrapper used by the engine for passive telemetry reads."""
    return PanasonicListener().read_state()


def set_panasonic_ac(power: bool, temp_c: int = 24, mode: str = "cool") -> None:
    """
    Sends a state command to the Panasonic AC via Comfort Cloud.

    Args:
        power:  True to turn ON, False for OFF.
        temp_c: Target temperature in Celsius (16–30).
        mode:   Operating mode — 'cool', 'heat', 'auto', 'fan', 'dry'.
    """
    session = pcomfortcloud.Session(
        username=config.PANASONIC_USERNAME,
        password=config.PANASONIC_PASSWORD,
    )

    try:
        session.login()

        parameters: dict = {
            "power": pcomfortcloud.constants.Power.On if power else pcomfortcloud.constants.Power.Off,
        }

        if power:
            parameters["temperature"] = max(16, min(30, temp_c))
            parameters["operationMode"] = _MODE_MAP.get(mode, pcomfortcloud.constants.OperationMode.Cool)

        session.set_device(config.PANASONIC_DEVICE_GUID, **parameters)
        log.info("Panasonic AC command sent — power=%s, temp=%d°C, mode=%s", power, temp_c, mode)

    except Exception as exc:
        raise RuntimeError(f"Panasonic AC command failed: {exc}") from exc

    finally:
        try:
            session.logout()
        except Exception:
            pass
