"""
engine.py — The Brain of GridLock OS
Implements the multi-variable predictive decision logic.

Decision Variables:
  - Battery SoC (%)            : from Solarman (current)
  - PV Yield (kW)              : from Solarman (current)
  - Load (kW)                  : from Solarman (current)
  - Forecasted Cloud Cover (%) : from OpenWeatherMap 3-h forecast @ ~17:00
  - Predicted SoC @ 17:00 (%)  : from predictor.py model/heuristic
  - Time of Day                : system clock

The Prime Directive (Predictive):
  If the model predicts SoC will be below PRIME_DIRECTIVE_SOC_TARGET (95%)
  at 17:00, the engine immediately shifts both ACs to 26°C — routing surplus
  PV yield to the battery instead of spending it on aggressive cooling.
  This fires during the day, BEFORE sunset, based on the prediction.
"""

import logging
from datetime import datetime

import config
from core.predictor import predict_soc_at_1700
from core.weather import get_forecast_for_1700
from db.time_series import write_snapshot
from integrations.gree import set_gree_ac
from integrations.panasonic import set_panasonic_ac
from integrations.solarman import get_telemetry

log = logging.getLogger("gridlock.engine")


# ── AC Setpoint Profiles ───────────────────────────────────────────────────────

class ACCommand:
    """Immutable command to send to an AC unit."""
    __slots__ = ("power", "temp_c", "mode", "fan_speed")

    def __init__(self, power: bool, temp_c: int = 24, mode: str = "cool", fan_speed: str = "auto"):
        self.power = power
        self.temp_c = temp_c
        self.mode = mode
        self.fan_speed = fan_speed

    def __repr__(self) -> str:
        state = "ON" if self.power else "OFF"
        return f"AC({state}, {self.temp_c}°C, {self.mode}, fan={self.fan_speed})"


def _decide(
    soc: float,
    cloud_cover: int,
    hour: int,
    predicted_soc_at_1700: float,
) -> ACCommand:
    """
    Core decision function. Returns an ACCommand applied to both units.

    Rules (evaluated top-to-bottom, first match wins):
    ┌────────────────────────────────────────────────────────────────────────┐
    │ 1. Battery critically low           → OFF (emergency floor)           │
    │ 2. Predictive Prime Directive fires → 26°C battery-save               │
    │    predicted_soc_at_1700 < PRIME_DIRECTIVE_SOC_TARGET (95%)           │
    │    Fires during the day regardless of current SoC.                    │
    │    Routes PV yield to battery instead of spending it on cooling.      │
    │ 3. Post-17:00, SoC below sunset target → OFF (depth-of-discharge      │
    │    protection)                                                         │
    │ 4. Post-17:00, SoC healthy          → 26°C light cooling              │
    │ 5. Daytime, heavy cloud (≥ 70 %)   → 25°C moderate cooling           │
    │ 6. Daytime, good solar              → 23°C aggressive pre-cool        │
    └────────────────────────────────────────────────────────────────────────┘
    """
    # Rule 1 — Emergency battery floor
    if soc < config.BATTERY_LOW_THRESHOLD:
        log.warning("SoC %.1f%% below floor %d%% — shutting down ACs.", soc, config.BATTERY_LOW_THRESHOLD)
        return ACCommand(power=False)

    # Rule 2 — Predictive Prime Directive
    if predicted_soc_at_1700 < config.PRIME_DIRECTIVE_SOC_TARGET:
        log.info(
            "Predictive Prime Directive: SoC@17:00 predicted %.1f%% < target %d%% — "
            "shifting ACs to 26°C to route PV to battery.",
            predicted_soc_at_1700,
            config.PRIME_DIRECTIVE_SOC_TARGET,
        )
        return ACCommand(power=True, temp_c=26, fan_speed="low")

    # Rule 3 & 4 — Post-17:00 reactive conservation
    if hour >= config.PRIME_DIRECTIVE_HOUR:
        if soc < config.BATTERY_SUNSET_TARGET:
            log.info("Post-17:00: SoC %.1f%% < sunset target %d%% — ACs OFF.", soc, config.BATTERY_SUNSET_TARGET)
            return ACCommand(power=False)
        log.info("Post-17:00: SoC %.1f%% healthy — light cooling.", soc)
        return ACCommand(power=True, temp_c=26, fan_speed="low")

    # Rule 5 — Daytime but heavily overcast; be conservative
    if cloud_cover >= 70:
        log.info("Heavy cloud cover (%d%%) — moderate cooling mode.", cloud_cover)
        return ACCommand(power=True, temp_c=25, fan_speed="auto")

    # Rule 6 — Good solar generation available; pre-cool aggressively
    log.info("Good solar (cloud=%d%%, SoC=%.1f%%) — aggressive cooling.", cloud_cover, soc)
    return ACCommand(power=True, temp_c=23, fan_speed="high")


def run_cycle() -> None:
    """
    Single execution cycle. Called by the scheduler in main.py every 5 minutes.
    1. Collect telemetry (SoC, PV yield, load)
    2. Fetch weather forecast for ~17:00
    3. Run predictive model
    4. Run decision logic
    5. Dispatch commands (skipped in DRY_RUN mode)
    6. Persist snapshot to InfluxDB
    """
    now = datetime.now()
    log.info("── Cycle start %s ──", now.strftime("%H:%M:%S"))

    # ── 1. Collect solar telemetry ────────────────────────────────────────────
    telemetry = get_telemetry()
    log.info(
        "Telemetry → SoC: %.1f%%, PV: %.2f kW, Load: %.2f kW",
        telemetry.soc, telemetry.pv_yield_kw, telemetry.load_kw,
    )

    # ── 2. Fetch 17:00 weather forecast ───────────────────────────────────────
    forecast = get_forecast_for_1700()

    # ── 3. Predict SoC at 17:00 ───────────────────────────────────────────────
    predicted_soc = predict_soc_at_1700(
        current_soc=telemetry.soc,
        current_load_kw=telemetry.load_kw,
        current_pv_yield_kw=telemetry.pv_yield_kw,
        forecasted_cloud_cover=forecast.cloud_cover,
    )
    log.info("Predicted SoC @ 17:00: %.1f%%", predicted_soc)

    # ── 4. Decide ─────────────────────────────────────────────────────────────
    command = _decide(
        soc=telemetry.soc,
        cloud_cover=forecast.cloud_cover,
        hour=now.hour,
        predicted_soc_at_1700=predicted_soc,
    )
    log.info("Decision → %s", command)

    # ── 5. Dispatch (honoring DRY_RUN) ────────────────────────────────────────
    if config.DRY_RUN:
        log.info("[DRY RUN] Skipping hardware dispatch — would send: %s", command)
    else:
        set_gree_ac(power=command.power, temp_c=command.temp_c, mode=command.mode, fan_speed=command.fan_speed)
        set_panasonic_ac(power=command.power, temp_c=command.temp_c, mode=command.mode)

    # ── 6. Persist ────────────────────────────────────────────────────────────
    write_snapshot(
        battery_soc=telemetry.soc,
        cloud_cover=forecast.cloud_cover,
        ac_power=command.power,
        ac_temp_setpoint=command.temp_c,
        predicted_soc_at_1700=predicted_soc,
    )

    log.info("── Cycle complete ──")
