"""Train an XGBoost regressor to predict battery SoC 1 hour ahead.

Usage:
    python src/ml/train_xgboost.py \
        --data-path gridlock_research_export.csv \
        --model-path src/ml/models/xgboost_soc_v1.json \
        --plot-path src/ml/models/feature_importance.png
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib

# Use a non-interactive backend for headless environments (Docker/servers).
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
from xgboost import XGBRegressor

STALE_FLAG_COLUMNS = [
    "gree_stale",
    "gree_state_fresh",
    "gree_stale_seconds",
    "gree_connect_failures",
]


def load_and_clean_data(csv_path: Path) -> pd.DataFrame:
    """Load CSV and perform data imputation for telemetry features."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Dataset not found: {csv_path}")

    df = pd.read_csv(csv_path)
    if "_time" not in df.columns:
        raise ValueError("Input dataset must include '_time' column")

    df["_time"] = pd.to_datetime(df["_time"], errors="coerce", utc=True)
    df = df.dropna(subset=["_time"]).sort_values("_time").reset_index(drop=True)

    # Explicitly zero-fill stale/health flags so missing states do not leak NaN.
    for col in STALE_FLAG_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

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
        df[sensor_cols] = df[sensor_cols].ffill().bfill()

    remaining_numeric = [c for c in numeric_cols if c not in sensor_cols and c not in STALE_FLAG_COLUMNS]
    for col in remaining_numeric:
        if df[col].isna().any():
            median = df[col].median()
            fill_value = 0 if pd.isna(median) else median
            df[col] = df[col].fillna(fill_value)

    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Create circular time features, lag features, and 1-hour-ahead target."""
    work = df.copy()

    if "battery_soc" not in work.columns:
        raise ValueError("Input dataset must include 'battery_soc' column")
    if "load_kw" not in work.columns:
        raise ValueError("Input dataset must include 'load_kw' column")

    # Circular hour encoding captures periodicity without discontinuity at midnight.
    hour_decimal = work["_time"].dt.hour + (work["_time"].dt.minute / 60.0)
    work["hour_sin"] = np.sin(2.0 * math.pi * hour_decimal / 24.0)
    work["hour_cos"] = np.cos(2.0 * math.pi * hour_decimal / 24.0)

    # 5-minute cadence -> 15-minute lag is 3 rows.
    work["battery_soc_lag_15m"] = work["battery_soc"].shift(3)
    work["load_kw_lag_15m"] = work["load_kw"].shift(3)

    # 5-minute cadence -> 1 hour ahead target is 12 rows.
    work["target_soc_1h"] = work["battery_soc"].shift(-12)

    work = work.dropna(subset=["battery_soc_lag_15m", "load_kw_lag_15m", "target_soc_1h"]).reset_index(drop=True)
    return work


def train_model(df: pd.DataFrame) -> tuple[XGBRegressor, float, list[str], np.ndarray]:
    """Train chronological split XGBoost regressor and return metrics/artifacts."""
    exclude_cols = {
        "_time",
        "target_soc_1h",
        "ac_gree_state",
        "ac_panasonic_state",
        "location",
        "result",
    }

    feature_df = df.drop(columns=[c for c in exclude_cols if c in df.columns], errors="ignore")

    # Keep only numeric features for XGBoost.
    X = feature_df.select_dtypes(include=[np.number]).copy()
    y = pd.to_numeric(df["target_soc_1h"], errors="coerce")

    if X.empty:
        raise ValueError("No numeric features available after preprocessing")

    split_idx = int(len(df) * 0.8)
    if split_idx <= 0 or split_idx >= len(df):
        raise ValueError("Dataset too small for 80/20 chronological split")

    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    model = XGBRegressor(
        objective="reg:squarederror",
        n_estimators=300,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    mae = mean_absolute_error(y_test, y_pred)

    return model, mae, X.columns.tolist(), model.feature_importances_


def save_feature_importance_plot(importances: np.ndarray, feature_names: list[str], output_path: Path) -> None:
    """Persist feature importance chart for model inspection."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    order = np.argsort(importances)[::-1]
    ordered_importances = importances[order]
    ordered_features = [feature_names[idx] for idx in order]

    plt.figure(figsize=(12, 7))
    plt.bar(range(len(ordered_features)), ordered_importances)
    plt.xticks(range(len(ordered_features)), ordered_features, rotation=75, ha="right")
    plt.title("XGBoost Feature Importance")
    plt.ylabel("Importance")
    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()


def run_pipeline(data_path: Path, model_path: Path, plot_path: Path) -> None:
    """Execute full training pipeline and artifact generation."""
    cleaned = load_and_clean_data(data_path)
    featured = engineer_features(cleaned)

    model, mae, feature_names, importances = train_model(featured)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))
    save_feature_importance_plot(importances, feature_names, plot_path)

    print(f"MAE: {mae:.4f}")
    print(f"Model saved to: {model_path}")
    print(f"Feature importance plot saved to: {plot_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train 1-hour-ahead battery SoC XGBoost model")
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path("gridlock_research_export.csv"),
        help="Path to exported GridLock telemetry CSV",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("src/ml/models/xgboost_soc_v1.json"),
        help="Path to save trained XGBoost model",
    )
    parser.add_argument(
        "--plot-path",
        type=Path,
        default=Path("src/ml/models/feature_importance.png"),
        help="Path to save feature importance chart",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_pipeline(args.data_path, args.model_path, args.plot_path)


if __name__ == "__main__":
    main()
