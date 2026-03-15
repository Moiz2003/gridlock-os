"""
weather.py — OpenWeatherMap 3-Hour Forecast Integration
Queries the OWM /forecast endpoint (5-day, 3-hour intervals) using lat/lon
for the configured location (default: Lahore, PK).

Public API:
    get_forecast_for_1700() -> ForecastSlot
        Returns the forecast slot nearest to today's 17:00, containing
        cloud_cover (%) and temp_c (°C). This is the primary input for
        the predictive SoC model in predictor.py.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time

import requests

import config

log = logging.getLogger("gridlock.weather")

_OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


@dataclass(frozen=True)
class ForecastSlot:
    """A single 3-hour forecast window from OpenWeatherMap."""
    cloud_cover: int    # 0–100 %
    temp_c: float       # degrees Celsius
    dt: datetime        # UTC timestamp of the forecast window


def get_forecast_for_1700() -> ForecastSlot:
    """
    Fetches the OWM 3-hour forecast and returns the slot whose timestamp is
    closest to today's 17:00 local time.

    Uses lat/lon from config (OWM_LATITUDE / OWM_LONGITUDE) so the result
    is precise regardless of city name ambiguity.

    Raises:
        RuntimeError: on network failure or unexpected API response shape.
    """
    params = {
        "lat": config.OWM_LATITUDE,
        "lon": config.OWM_LONGITUDE,
        "appid": config.OWM_API_KEY,
        "units": "metric",
        "cnt": 16,  # 16 × 3 h = 48 h of data — enough to cover today's 17:00
    }

    try:
        response = requests.get(_OWM_FORECAST_URL, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenWeatherMap forecast request failed: {exc}") from exc

    slots = response.json().get("list", [])
    if not slots:
        raise RuntimeError("OpenWeatherMap returned an empty forecast list.")

    target_dt = datetime.combine(date.today(), dt_time(hour=config.PRIME_DIRECTIVE_HOUR, minute=0))

    def _delta_seconds(entry: dict) -> float:
        entry_dt = datetime.fromtimestamp(entry["dt"])
        return abs((entry_dt - target_dt).total_seconds())

    nearest = min(slots, key=_delta_seconds)
    slot = ForecastSlot(
        cloud_cover=nearest["clouds"]["all"],
        temp_c=nearest["main"]["temp"],
        dt=datetime.fromtimestamp(nearest["dt"]),
    )

    log.info(
        "Forecast @ ~17:00 → cloud_cover=%d%%, temp=%.1f°C (slot dt=%s)",
        slot.cloud_cover,
        slot.temp_c,
        slot.dt.strftime("%H:%M"),
    )
    return slot
