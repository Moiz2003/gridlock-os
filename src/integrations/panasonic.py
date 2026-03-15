"""
panasonic.py — Panasonic Comfort Cloud Integration
Sends commands to the Panasonic AC via the Comfort Cloud cloud API
using the pcomfortcloud library.

This module only sends commands. It does not make decisions.
"""

import logging

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
