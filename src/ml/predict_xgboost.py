"""Standalone inference pipeline for 1-hour-ahead battery SoC prediction.

Usage:
    python src/ml/predict_xgboost.py
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient
from xgboost import XGBRegressor

try:
    from src.ml.train_xgboost import STALE_FLAG_COLUMNS
except ModuleNotFoundError:
    # Support direct execution when repository root is not on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.ml.train_xgboost import STALE_FLAG_COLUMNS

LOGGER = logging.getLogger("gridlock.ml.predict")

MODEL_PATH = Path("src/ml/models/xgboost_soc_v1.json")
DEFAULT_BUCKET = "home_energy"
DEFAULT_MEASUREMENT = "gridlock_snapshot"
LOOKBACK_MINUTES = 20


def _normalize_influx_url(url: str) -> str:
    """Resolve Influx URL for both host and container execution contexts."""
    normalized = url or "http://localhost:8086"
    if "gridlock-influxdb" in normalized:
        return normalized.replace("gridlock-influxdb", "localhost")
    if "influxdb" in normalized:
        return normalized.replace("influxdb", "localhost")
    return normalized


def _query_recent_window(client: InfluxDBClient, bucket: str, measurement: str) -> pd.DataFrame:
    """Fetch and pivot the most recent telemetry window from InfluxDB."""
    query = f'''
from(bucket: "{bucket}")
  |> range(start: -{LOOKBACK_MINUTES}m)
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> drop(columns: ["_start", "_stop", "_measurement"])
'''

    raw = client.query_api().query_data_frame(query)
    if isinstance(raw, list):
        frames = [df for df in raw if isinstance(df, pd.DataFrame) and not df.empty]
        if not frames:
            return pd.DataFrame()
        data = pd.concat(frames, ignore_index=True)
    else:
        data = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame()

    return data


def _clean_recent_data(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the same training-style imputation rules to recent telemetry."""
    if df.empty:
        return df

    work = df.copy()
    if "_time" in work.columns:
        work["_time"] = pd.to_datetime(work["_time"], errors="coerce", utc=True)
        work = work.dropna(subset=["_time"]).sort_values("_time").reset_index(drop=True)

    for col in STALE_FLAG_COLUMNS:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    numeric_cols = work.select_dtypes(include=[np.number]).columns.tolist()

    sensor_keywords = (
        "temp",
        "soc",
        "load",
        "pv",
        "power",
        "cloud",
        "forecast",
        "potential",
        "efficiency",
        "energy",
    )

    sensor_cols = [
        c
        for c in numeric_cols
        if c not in STALE_FLAG_COLUMNS and any(keyword in c for keyword in sensor_keywords)
    ]

    if sensor_cols:
        work[sensor_cols] = work[sensor_cols].ffill().bfill()

    remaining_numeric = [c for c in numeric_cols if c not in sensor_cols and c not in STALE_FLAG_COLUMNS]
    for col in remaining_numeric:
        if work[col].isna().any():
            median = work[col].median()
            fill_value = 0 if pd.isna(median) else median
            work[col] = work[col].fillna(fill_value)

    return work


def _resolve_feature_names(model: XGBRegressor) -> list[str]:
    """Get training feature names from the saved XGBoost artifact."""
    feature_names = []
    if hasattr(model, "feature_names_in_"):
        feature_names = [str(name) for name in getattr(model, "feature_names_in_")]

    if not feature_names:
        booster = model.get_booster()
        feature_names = list(booster.feature_names or [])

    if not feature_names:
        raise RuntimeError("Unable to resolve model feature names from saved artifact")

    return feature_names


def _safe_numeric(series: pd.Series, fallback: float = 0.0) -> float:
    value = pd.to_numeric(series, errors="coerce")
    if pd.isna(value):
        return float(fallback)
    return float(value)


def _extract_lag_value(df: pd.DataFrame, field_name: str, reference_time: pd.Timestamp) -> float:
    """Extract value from approximately 15 minutes before reference time."""
    if field_name not in df.columns or "_time" not in df.columns or df.empty:
        LOGGER.warning("Missing lag source '%s'; using fallback 0.0", field_name)
        return 0.0

    candidate = df[df["_time"] <= (reference_time - pd.Timedelta(minutes=15))]
    if candidate.empty:
        LOGGER.warning("No 15-minute history for '%s'; using oldest recent value", field_name)
        return _safe_numeric(df.iloc[0][field_name], 0.0)

    return _safe_numeric(candidate.iloc[-1][field_name], 0.0)


def _build_feature_vector(df: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    """Create a single-row feature frame aligned to model's expected columns."""
    if df.empty:
        raise RuntimeError("No recent telemetry available to build inference feature vector")

    latest = df.iloc[-1]
    now = pd.Timestamp.now(tz="UTC")
    hour_decimal = now.hour + now.minute / 60.0 + now.second / 3600.0

    lag_soc = _extract_lag_value(df, "battery_soc", now)
    lag_load = _extract_lag_value(df, "load_kw", now)

    row: dict[str, float] = {}
    for feature in feature_names:
        if feature == "hour_sin":
            row[feature] = float(np.sin(2.0 * math.pi * hour_decimal / 24.0))
            continue
        if feature == "hour_cos":
            row[feature] = float(np.cos(2.0 * math.pi * hour_decimal / 24.0))
            continue
        if feature == "battery_soc_lag_15m":
            row[feature] = lag_soc
            continue
        if feature == "load_kw_lag_15m":
            row[feature] = lag_load
            continue

        if feature in df.columns:
            row[feature] = _safe_numeric(latest[feature], np.nan)
        else:
            LOGGER.warning("Feature '%s' missing from recent window; applying fallback", feature)
            row[feature] = np.nan

    vector = pd.DataFrame([row], columns=feature_names)

    # Final defensive fill for any unresolved NaN values.
    for col in vector.columns:
        if pd.isna(vector.at[0, col]):
            if col in STALE_FLAG_COLUMNS:
                vector.at[0, col] = 0.0
                continue

            if col in df.columns:
                median = pd.to_numeric(df[col], errors="coerce").median()
                vector.at[0, col] = 0.0 if pd.isna(median) else float(median)
            else:
                vector.at[0, col] = 0.0

    return vector.astype(float)


def predict_next_hour_soc(client: InfluxDBClient) -> float:
    """Predict battery SoC 1 hour from now using the trained XGBoost model."""
    bucket = os.getenv("INFLUXDB_BUCKET", DEFAULT_BUCKET)
    measurement = os.getenv("INFLUXDB_MEASUREMENT", DEFAULT_MEASUREMENT)

    model = XGBRegressor()
    model.load_model(str(MODEL_PATH))

    feature_names = _resolve_feature_names(model)

    recent = _query_recent_window(client, bucket=bucket, measurement=measurement)
    clean_recent = _clean_recent_data(recent)
    feature_vector = _build_feature_vector(clean_recent, feature_names)

    prediction = model.predict(feature_vector)[0]
    return float(prediction)


def _build_client_from_env() -> InfluxDBClient:
    load_dotenv()

    influx_url = _normalize_influx_url(os.getenv("INFLUXDB_URL", "http://localhost:8086"))
    influx_token = os.getenv("INFLUXDB_TOKEN")
    influx_org = os.getenv("INFLUXDB_ORG", "gridlock")

    if not influx_token:
        raise RuntimeError("INFLUXDB_TOKEN is required in environment/.env")

    return InfluxDBClient(url=influx_url, token=influx_token, org=influx_org, timeout=30000)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")

    try:
        client = _build_client_from_env()
        prediction = predict_next_hour_soc(client)
        print(f"[AI PREDICTION] Battery SoC in 1 Hour: {prediction:.1f}%")
    except Exception as exc:
        LOGGER.exception("Prediction failed: %s", exc)
        raise
