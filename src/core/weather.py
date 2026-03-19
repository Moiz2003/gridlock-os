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
from datetime import date, datetime, time as dt_time, timedelta

import requests

import config

log = logging.getLogger("gridlock.weather")

_OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
_OWM_CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"


@dataclass(frozen=True)
class ForecastSlot:
    """A single 3-hour forecast window from OpenWeatherMap."""
    cloud_cover: int                 # 0–100 %
    temp_c: float                    # degrees Celsius
    dt: datetime                     # UTC timestamp of the forecast window
    theoretical_pv_potential_kw: float
    forecast_max_temp_3d_c: float
    heatwave_detected_3d: bool


@dataclass(frozen=True)
class CurrentWeather:
    temp_c: float
    cloud_cover: int
    theoretical_pv_potential_kw: float


def _calc_theoretical_pv_potential_kw(cloud_cover: int) -> float:
    """Estimate PV potential using cloud dampening against array nameplate."""
    pmax_kw = float(config.PV_ARRAY_CAPACITY_KW)
    cloud_fraction = max(0.0, min(1.0, cloud_cover / 100.0))
    potential_kw = pmax_kw * (1.0 - 0.75 * cloud_fraction)
    # Keep value in physically valid range [0, Pmax].
    return max(0.0, min(pmax_kw, potential_kw))


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
    max_window_end = datetime.now() + timedelta(days=3)
    temps_3d = [entry["main"]["temp_max"] for entry in slots if datetime.fromtimestamp(entry["dt"]) <= max_window_end]
    max_temp_3d_c = float(max(temps_3d)) if temps_3d else float(nearest["main"]["temp"])
    heatwave_detected = max_temp_3d_c > 38.0

    theoretical_pv_potential_kw = _calc_theoretical_pv_potential_kw(nearest["clouds"]["all"])

    slot = ForecastSlot(
        cloud_cover=nearest["clouds"]["all"],
        temp_c=nearest["main"]["temp"],
        dt=datetime.fromtimestamp(nearest["dt"]),
        theoretical_pv_potential_kw=theoretical_pv_potential_kw,
        forecast_max_temp_3d_c=max_temp_3d_c,
        heatwave_detected_3d=heatwave_detected,
    )

    log.info(
        "Forecast @ ~17:00 → cloud_cover=%d%%, temp=%.1f°C, max_3d=%.1f°C, pv_potential=%.2f kW (slot dt=%s)",
        slot.cloud_cover,
        slot.temp_c,
        slot.forecast_max_temp_3d_c,
        slot.theoretical_pv_potential_kw,
        slot.dt.strftime("%H:%M"),
    )
    return slot


def get_current_weather() -> float:
    """
    Fetches current weather from OWM and returns current outside temperature
    in Celsius.

    Raises:
        RuntimeError: on network failure or unexpected API response shape.
    """
    params = {
        "lat": config.OWM_LATITUDE,
        "lon": config.OWM_LONGITUDE,
        "appid": config.OWM_API_KEY,
        "units": "metric",
    }

    try:
        response = requests.get(_OWM_CURRENT_URL, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenWeatherMap current weather request failed: {exc}") from exc

    data = response.json()
    try:
        current_temp = float(data["main"]["temp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenWeatherMap current weather response missing main.temp") from exc

    log.info("Current weather → temp=%.1f°C", current_temp)
    return current_temp


def get_current_conditions() -> CurrentWeather:
    """Fetch current temperature and cloud cover, plus PV potential estimate."""
    params = {
        "lat": config.OWM_LATITUDE,
        "lon": config.OWM_LONGITUDE,
        "appid": config.OWM_API_KEY,
        "units": "metric",
    }

    try:
        response = requests.get(_OWM_CURRENT_URL, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenWeatherMap current weather request failed: {exc}") from exc

    data = response.json()
    try:
        temp_c = float(data["main"]["temp"])
        cloud_cover = int(data["clouds"]["all"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OpenWeatherMap current weather response missing main.temp/clouds.all") from exc

    theoretical_pv_potential_kw = _calc_theoretical_pv_potential_kw(cloud_cover)

    log.info(
        "PV potential calc (current) → pmax=%.2f kW, cloud_cover=%d%%, potential=%.2f kW",
        config.PV_ARRAY_CAPACITY_KW,
        cloud_cover,
        theoretical_pv_potential_kw,
    )

    snapshot = CurrentWeather(
        temp_c=temp_c,
        cloud_cover=cloud_cover,
        theoretical_pv_potential_kw=theoretical_pv_potential_kw,
    )
    log.info(
        "Current weather → temp=%.1f°C, cloud_cover=%d%%, pv_potential=%.2f kW",
        snapshot.temp_c,
        snapshot.cloud_cover,
        snapshot.theoretical_pv_potential_kw,
    )
    return snapshot
