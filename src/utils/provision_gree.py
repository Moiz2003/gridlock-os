"""One-time Gree discovery/bind utility for the HUAWEI-act2 network.

This script uses greeclimate's supported capabilities:
1. verify the host is on the target Wi-Fi,
2. discover reachable Gree units on that LAN,
3. bind to a selected unit and print the device key for .env.

Note: the installed greeclimate library does not expose SSID/password onboarding
for moving an AC from hotspot mode onto home Wi-Fi. If the unit is still in AP
mode, first join it to HUAWEI-act2 using the vendor app or hotspot workflow,
then run this script to finish local-network setup for GridLock.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

from greeclimate.device import Device
from greeclimate.discovery import Discovery

WIFI_SSID = "HUAWEI-act2"
WIFI_PASSWORD = "uBVWzt8p"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("gridlock.provision_gree")


def _check_current_wifi() -> None:
    """Best-effort validation that the host is on the expected Wi-Fi."""
    if not shutil.which("nmcli"):
        log.warning("nmcli not found; skipping local Wi-Fi verification. Expected SSID: %s", WIFI_SSID)
        return

    result = subprocess.run(
        ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
        capture_output=True,
        text=True,
        check=False,
    )
    active_ssid = ""
    for line in result.stdout.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and parts[0] == "yes":
            active_ssid = parts[1]
            break

    if active_ssid == WIFI_SSID:
        log.info("Host Wi-Fi verified on %s", WIFI_SSID)
        return

    log.warning(
        "Host Wi-Fi is '%s', expected '%s'. Password prepared: %s",
        active_ssid or "unknown",
        WIFI_SSID,
        WIFI_PASSWORD,
    )


async def _discover_and_bind() -> int:
    discovery = Discovery(timeout=3)
    devices = await discovery.scan(wait_for=3)

    if not devices:
        log.error("No Gree devices discovered on %s.", WIFI_SSID)
        log.error("If the unit is still in hotspot mode, join it to %s first using the vendor workflow.", WIFI_SSID)
        return 1

    device_info = devices[0]
    log.info("Discovered Gree unit: %s", device_info)

    device = Device(device_info)
    await device.bind()
    await device.update_state()

    log.info("Bind successful.")
    log.info("Use these values in .env:")
    log.info("GREE_AC_IP=%s", device_info.ip)
    log.info("GREE_AC_PORT=%s", device_info.port)
    log.info("GREE_AC_MAC=%s", device_info.mac)
    log.info("GREE_AC_KEY=%s", device.device_key)
    log.info(
        "Current state: power=%s target_temp=%s current_temp=%s fan_speed=%s",
        device.power,
        device.target_temperature,
        device.current_temperature,
        device.fan_speed,
    )
    return 0


def main() -> int:
    _check_current_wifi()
    return asyncio.run(_discover_and_bind())


if __name__ == "__main__":
    raise SystemExit(main())