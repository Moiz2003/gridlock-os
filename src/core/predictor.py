"""
predictor.py — Predictive SoC Model Wrapper
Estimates the battery State of Charge at 17:00 today given current telemetry
and the OWM cloud-cover forecast.

Model Strategy (two-tier):
  1. Trained model (preferred): If a serialised sklearn / XGBoost model file
     exists at config.MODEL_PATH it is loaded with joblib and used for
     inference.  Expected feature order:
       [current_soc, current_load_kw, current_pv_yield_kw, forecasted_cloud_cover]
     Expected output: predicted SoC at 17:00 (float, 0–100).

  2. Physics heuristic (fallback): When no model file is present the function
     falls back to a deterministic linear energy-balance calculation:
       effective_pv = current_pv_yield × (1 − cloud_cover / 100)
       net_power    = effective_pv − current_load
       delta_soc    = (net_power × hours_to_1700 / BATTERY_CAPACITY_KWH) × 100
       prediction   = clamp(current_soc + delta_soc, 0, 100)
"""

import logging
import os
from datetime import datetime

import numpy as np

import config

log = logging.getLogger("gridlock.predictor")

# Module-level model cache — loaded once, reused every cycle
_model = None
_model_checked: bool = False


def _load_model():
    """
    Attempts to load a joblib-serialised model from config.MODEL_PATH.
    Returns the model object, or None if the path is not configured / missing.
    Caches the result so disk I/O only happens once per process lifetime.
    """
    global _model, _model_checked
    if _model_checked:
        return _model

    _model_checked = True
    model_path = config.MODEL_PATH

    if not model_path:
        log.info("MODEL_PATH not set — predictor will use physics heuristic.")
        return None

    if not os.path.exists(model_path):
        log.warning("Model file not found at '%s' — using physics heuristic.", model_path)
        return None

    try:
        import joblib  # noqa: PLC0415 — intentional lazy import
        _model = joblib.load(model_path)
        log.info("Predictive model loaded from '%s'.", model_path)
    except Exception as exc:
        log.error("Failed to load model from '%s': %s — using heuristic.", model_path, exc)
        _model = None

    return _model


def _physics_heuristic(
    current_soc: float,
    current_load_kw: float,
    current_pv_yield_kw: float,
    forecasted_cloud_cover: int,
) -> float:
    """
    Linear energy-balance model.
    Projects the current net power (PV − load, cloud-adjusted) forward to 17:00
    and converts the delta energy into a SoC change against the known battery capacity.
    """
    now = datetime.now()
    # Fractional hours remaining until 17:00 (floor to 0 if already past)
    hours_to_1700 = max(0.0, config.PRIME_DIRECTIVE_HOUR - (now.hour + now.minute / 60.0))

    cloud_factor = 1.0 - (forecasted_cloud_cover / 100.0)
    effective_pv_kw = current_pv_yield_kw * cloud_factor
    net_power_kw = effective_pv_kw - current_load_kw

    delta_energy_kwh = net_power_kw * hours_to_1700
    delta_soc_pct = (delta_energy_kwh / config.BATTERY_CAPACITY_KWH) * 100.0

    predicted = max(0.0, min(100.0, current_soc + delta_soc_pct))

    log.debug(
        "Heuristic: %.1f h to 17:00, PV_eff=%.2f kW, net=%.2f kW, "
        "Δ=%.1f%%, predicted SoC=%.1f%%",
        hours_to_1700, effective_pv_kw, net_power_kw, delta_soc_pct, predicted,
    )
    return predicted


def predict_soc_at_1700(
    current_soc: float,
    current_load_kw: float,
    current_pv_yield_kw: float,
    forecasted_cloud_cover: int,
) -> float:
    """
    Returns the predicted battery SoC (%) at 17:00 today.

    Args:
        current_soc:           Present battery SoC, 0–100 %.
        current_load_kw:       Present household load in kilowatts.
        current_pv_yield_kw:   Present PV generation in kilowatts.
        forecasted_cloud_cover: OWM forecast cloud cover for ~17:00, 0–100 %.

    Returns:
        Predicted SoC at 17:00 as a float clamped to [0, 100].
    """
    model = _load_model()

    if model is not None:
        features = np.array(
            [[current_soc, current_load_kw, current_pv_yield_kw, forecasted_cloud_cover]],
            dtype=np.float32,
        )
        try:
            prediction = float(model.predict(features)[0])
            prediction = max(0.0, min(100.0, prediction))
            log.info("Model prediction → SoC@17:00 = %.1f%%", prediction)
            return prediction
        except Exception as exc:
            log.error("Model inference failed (%s) — falling back to heuristic.", exc)

    prediction = _physics_heuristic(current_soc, current_load_kw, current_pv_yield_kw, forecasted_cloud_cover)
    log.info("Heuristic prediction → SoC@17:00 = %.1f%%", prediction)
    return prediction
