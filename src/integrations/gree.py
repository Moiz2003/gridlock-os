"""
gree.py — Gree AC Local Network Integration
Sends commands to the Gree AC over the local Wi-Fi network using the
greeclimate library, which implements the Gree UDP protocol.

This module only sends commands. It does not make decisions.
"""

import logging

from greeclimate.device import Device, DeviceInfo, Mode, FanSpeed
from greeclimate.network import IPAddr

import config

log = logging.getLogger("gridlock.gree")

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
        ip=IPAddr(config.GREE_AC_IP),
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
