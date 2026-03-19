"""
discord.py — Lightweight Discord Webhook wrapper.
Never raises outward; alert delivery must not impact control loop execution.
"""

import logging

log = logging.getLogger("gridlock.discord")


def send_alert(message: str) -> None:
    """
    Bake-mode no-op: alerts are intentionally silenced for uninterrupted data capture.
    """
    log.debug("[BAKE MODE] Alert suppressed: %s", message)
