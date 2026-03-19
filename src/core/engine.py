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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import config
from core.predictor import predict_soc_at_1700
from core.weather import get_current_conditions, get_forecast_for_1700
from db.time_series import write_snapshot
from integrations.discord import send_alert
from integrations.gree import get_gree_state
from integrations.panasonic import get_panasonic_state
from integrations.solarman import SolarTelemetry, get_telemetry
from pysolarmanv5 import NoSocketAvailableError

log = logging.getLogger("gridlock.engine")


@dataclass
class EngineState:
    is_clipping: bool = False
    solar_health_persist_cycles: int = 0
    heatwave_notified: bool = False
    manual_override_until: datetime | None = None


state = EngineState()

# Hard guard for bake phase: this engine must never send AC control packets.
PASSIVE_AC_LISTEN_ONLY = True
GREE_STALE_ALERT_SECONDS = 300
MANUAL_OVERRIDE_COOLDOWN_MINUTES = 60


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
    try:
        telemetry = get_telemetry()
    except NoSocketAvailableError as exc:
        log.warning("[INVERTER_OFFLINE] Engine caught NoSocketAvailableError; using zeroed telemetry: %s", exc)
        telemetry = SolarTelemetry(soc=0.0, pv_yield_kw=0.0, load_kw=0.0)
    except OSError as exc:
        if exc.errno == 101:
            log.warning("[INVERTER_OFFLINE] Engine caught Errno 101; using zeroed telemetry: %s", exc)
            telemetry = SolarTelemetry(soc=0.0, pv_yield_kw=0.0, load_kw=0.0)
        else:
            raise
    inverter_offline = telemetry.soc == 0.0 and telemetry.pv_yield_kw == 0.0 and telemetry.load_kw == 0.0
    log.info(
        "Telemetry → SoC: %.1f%%, PV: %.2f kW, Load: %.2f kW",
        telemetry.soc, telemetry.pv_yield_kw, telemetry.load_kw,
    )
    if inverter_offline:
        log.warning("[INVERTER_OFFLINE] Engine received zeroed telemetry fallback; continuing with conservative logic.")

    # ── 2. Fetch 17:00 weather forecast ───────────────────────────────────────
    forecast = get_forecast_for_1700()

    # ── 2b. Fetch current outside weather for snapshot + health logic ────────
    current_weather = get_current_conditions()
    current_temp = current_weather.temp_c

    # ── 3. Predict SoC at 17:00 ───────────────────────────────────────────────
    predicted_soc = predict_soc_at_1700(
        current_soc=telemetry.soc,
        current_load_kw=telemetry.load_kw,
        current_pv_yield_kw=telemetry.pv_yield_kw,
        forecasted_cloud_cover=forecast.cloud_cover,
    )
    log.info("Predicted SoC @ 17:00: %.1f%%", predicted_soc)

    # ── 3b. Detect clipping and solar health performance ─────────────────────
    theoretical_pv_potential = current_weather.theoretical_pv_potential_kw
    # Safety guard: if a bad mapping ever mirrors cloud %% into potential, correct it before persistence.
    if abs(theoretical_pv_potential - float(current_weather.cloud_cover)) < 0.01:
        theoretical_pv_potential = max(
            0.0,
            min(
                config.PV_ARRAY_CAPACITY_KW,
                config.PV_ARRAY_CAPACITY_KW * (1.0 - 0.75 * (current_weather.cloud_cover / 100.0)),
            ),
        )
        log.warning(
            "Corrected mirrored theoretical_pv_potential value. cloud_cover=%d%%, corrected_potential=%.2f kW",
            current_weather.cloud_cover,
            theoretical_pv_potential,
        )
    potential_denominator = max(theoretical_pv_potential, 0.001)
    solar_health_score = 0.0 if inverter_offline else telemetry.pv_yield_kw / potential_denominator

    clipping_now = False if inverter_offline else telemetry.soc > 98.0 and telemetry.pv_yield_kw < (theoretical_pv_potential * 0.5)
    solar_health_alert_now = (
        False
        if inverter_offline
        else (
            current_weather.cloud_cover < 10
            and telemetry.pv_yield_kw < (theoretical_pv_potential * 0.8)
            and not clipping_now
        )
    )

    log.info(
        "PV diagnostic → theoretical_pv_potential=%.2f kW, actual_pv=%.2f kW",
        theoretical_pv_potential,
        telemetry.pv_yield_kw,
    )

    if clipping_now and not state.is_clipping:
        send_alert(
            "GridLock alert: PV clipping likely started. Consider running a daytime AC blast to dump excess solar."
        )

    state.is_clipping = clipping_now

    if solar_health_alert_now:
        state.solar_health_persist_cycles += 1
        if state.solar_health_persist_cycles == 3:
            send_alert(
                "GridLock alert: Solar underperformance persisted for 3 cycles under clear skies. Panel wash recommended."
            )
    else:
        state.solar_health_persist_cycles = 0

    log.info("Dust audit counter → %d", state.solar_health_persist_cycles)

    if forecast.heatwave_detected_3d and not state.heatwave_notified:
        send_alert(
            f"GridLock alert: Heatwave risk detected in 3-day forecast (max {forecast.forecast_max_temp_3d_c:.1f}C). "
            "Consider temporarily raising battery reserve floor."
        )
        state.heatwave_notified = True
    elif not forecast.heatwave_detected_3d:
        state.heatwave_notified = False

    # ── 4. Decide ─────────────────────────────────────────────────────────────
    command = _decide(
        soc=telemetry.soc,
        cloud_cover=forecast.cloud_cover,
        hour=now.hour,
        predicted_soc_at_1700=predicted_soc,
    )

    # Proactive override: use flexible load early afternoon if clipping is active.
    if state.is_clipping and now.hour < 15:
        command = ACCommand(power=True, temp_c=16, mode="cool", fan_speed="high")
        log.info("Solar Dump Mode override active (clipping detected before 15:00).")
    else:
        if state.is_clipping and now.hour >= 15:
            log.info("Solar Dump Mode suppressed (clipping detected but hour=%02d >= 15).", now.hour)
        elif not state.is_clipping:
            log.info("Solar Dump Mode suppressed (no clipping condition).")

    # ── 5. AC passive telemetry sync ─────────────────────────────────────────
    ac_gree_state: dict[str, Any] = {}
    ac_panasonic_state: dict[str, Any] = {}

    try:
        ac_gree_state = get_gree_state()
    except Exception as exc:
        log.warning("[HW_OFFLINE] Gree listener unavailable: %s", exc)

    try:
        ac_panasonic_state = get_panasonic_state()
    except Exception as exc:
        log.warning("[HW_OFFLINE] Panasonic listener unavailable: %s", exc)

    log.info("[AC_SYNC] Gree: %s | Panasonic: %s", ac_gree_state or "offline", ac_panasonic_state or "offline")

    # Human-in-the-loop safety: when observed state conflicts with desired ON,
    # yield for a cooldown window to avoid split-brain command intent.
    if state.manual_override_until is not None and now >= state.manual_override_until:
        log.info("[YIELD_MODE] Manual override cooldown expired.")
        state.manual_override_until = None

    gree_power_observed = ac_gree_state.get("power")
    manual_override_active = state.manual_override_until is not None and now < state.manual_override_until
    if isinstance(gree_power_observed, bool) and not gree_power_observed and command.power:
        if not manual_override_active:
            state.manual_override_until = now + timedelta(minutes=MANUAL_OVERRIDE_COOLDOWN_MINUTES)
            manual_override_active = True
            log.warning(
                "[MANUAL_OVERRIDE] Observed AC OFF while AI desired ON; yielding for %d minutes.",
                MANUAL_OVERRIDE_COOLDOWN_MINUTES,
            )

    if manual_override_active:
        remaining_seconds = max(0, int((state.manual_override_until - now).total_seconds()))
        command = ACCommand(power=False, temp_c=command.temp_c, mode=command.mode, fan_speed=command.fan_speed)
        log.warning("[YIELD_MODE] Active (%ds remaining); forcing effective command to AC OFF.", remaining_seconds)

    log.info("Decision → %s", command)

    gree_stale_seconds = ac_gree_state.get("stale_seconds")
    if isinstance(gree_stale_seconds, (int, float)) and gree_stale_seconds > GREE_STALE_ALERT_SECONDS:
        log.warning("[AC_STALE] Gree state stale for %.0fs (> %ss)", gree_stale_seconds, GREE_STALE_ALERT_SECONDS)

    # ── 6. Dispatch (hard-disabled during passive bake mode) ────────────────
    if PASSIVE_AC_LISTEN_ONLY:
        log.info("[PASSIVE_GUARD] Control dispatch disabled — listen-only mode active. Decision was: %s", command)
    elif config.DRY_RUN:
        log.info("[DRY RUN] Skipping hardware dispatch — would send: %s", command)
    else:
        # This path is intentionally unreachable in passive bake mode.
        log.warning("Dispatch path reached while passive mode is disabled.")

    # ── 7. Persist ────────────────────────────────────────────────────────────
    write_snapshot(
        battery_soc=telemetry.soc,
        cloud_cover=forecast.cloud_cover,
        outside_temp_c=current_temp,
        ac_power=command.power,
        ac_temp_setpoint=command.temp_c,
        predicted_soc_at_1700=predicted_soc,
        pv_yield_kw=telemetry.pv_yield_kw,
        load_kw=telemetry.load_kw,
        ac_output_power_kw=telemetry.ac_output_power_kw,
        daily_pv_energy_kwh=telemetry.daily_pv_energy_kwh,
        daily_load_energy_kwh=telemetry.daily_load_energy_kwh,
        total_energy_kwh=telemetry.total_energy_kwh,
        inverter_efficiency=telemetry.inverter_efficiency,
        theoretical_pv_potential=theoretical_pv_potential,
        is_clipping=state.is_clipping,
        solar_health_score=solar_health_score,
        forecast_max_temp_3d=forecast.forecast_max_temp_3d_c,
        ac_gree_state=ac_gree_state,
        ac_panasonic_state=ac_panasonic_state,
    )

    log.info("── Cycle complete ──")
