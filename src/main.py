"""
main.py — GridLock OS Entry Point
Sets up the 5-minute heartbeat scheduler and delegates all logic to engine.py.
This file has almost no decisions in it — it only drives the clock.
"""

import logging
import schedule
import time

from core.engine import run_cycle

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gridlock.main")


def _safe_run_cycle() -> None:
    """Wraps run_cycle so a single failure never kills the scheduler."""
    try:
        run_cycle()
    except Exception as exc:
        log.error("Cycle failed — will retry in 5 minutes. Reason: %s", exc, exc_info=True)


def main() -> None:
    log.info("GridLock OS starting up...")

    # Run once immediately on boot so we don't wait 5 minutes for first data
    _safe_run_cycle()

    # Schedule every 5 minutes from here on
    schedule.every(5).minutes.do(_safe_run_cycle)
    log.info("Scheduler armed — running every 5 minutes.")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
