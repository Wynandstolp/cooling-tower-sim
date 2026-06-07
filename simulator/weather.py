"""
Weather data loader for the cooling tower simulator.

Fetches hourly dry bulb and wet bulb temperatures from the Open-Meteo
historical weather API (https://open-meteo.com), which provides ERA5
reanalysis data at ~9 km resolution for any Australian location.

Downloaded data is cached as Parquet files in data/bom_weather/ to avoid
repeated API calls.

Typical usage::

    from simulator.weather import fetch_weather, QUEENSLAND_LOCATIONS

    df = fetch_weather("2023-01-01", "2023-12-31")          # Rockhampton default
    df = fetch_weather("2023-01-01", "2023-12-31",
                       **QUEENSLAND_LOCATIONS["townsville"])
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Queensland location presets
# ---------------------------------------------------------------------------

QUEENSLAND_LOCATIONS: dict[str, dict[str, Any]] = {
    "rockhampton": {
        "latitude": -23.38,
        "longitude": 150.52,
        "label": "Rockhampton",
    },
    "townsville": {
        "latitude": -19.25,
        "longitude": 146.77,
        "label": "Townsville",
    },
    "mt_isa": {
        "latitude": -20.73,
        "longitude": 139.49,
        "label": "Mt Isa",
    },
    "mackay": {
        "latitude": -21.12,
        "longitude": 149.22,
        "label": "Mackay",
    },
    "gladstone": {
        "latitude": -23.84,
        "longitude": 151.26,
        "label": "Gladstone",
    },
}

_DEFAULT_LOCATION = QUEENSLAND_LOCATIONS["rockhampton"]
_CACHE_DIR = Path("data/bom_weather")
_API_URL = "https://archive-api.open-meteo.com/v1/archive"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_weather(
    start_date: str,
    end_date: str,
    latitude: float = _DEFAULT_LOCATION["latitude"],
    longitude: float = _DEFAULT_LOCATION["longitude"],
    label: str = _DEFAULT_LOCATION["label"],
    cache_dir: Path = _CACHE_DIR,
) -> pd.DataFrame:
    """
    Return hourly weather data for the given date range and location.

    Columns returned:
        timestamp (DatetimeTZDtype, UTC+10)  — start of each hour
        t_amb     (float64, °C)              — dry bulb temperature
        t_wb      (float64, °C)              — wet bulb temperature

    Data is fetched once and cached as Parquet. Subsequent calls for the
    same location/range are served from the cache.

    Args:
        start_date: ISO date string, e.g. "2023-01-01"
        end_date:   ISO date string, e.g. "2023-12-31" (inclusive)
        latitude:   Decimal degrees (negative = south)
        longitude:  Decimal degrees
        label:      Human-readable location name, used in cache filename
        cache_dir:  Directory for cached Parquet files

    Returns:
        DataFrame with columns [timestamp, t_amb, t_wb], indexed 0..N-1
    """
    cache_path = _cache_path(cache_dir, label, start_date, end_date)

    if cache_path.exists():
        return pd.read_parquet(cache_path)

    df = _fetch_from_api(latitude, longitude, start_date, end_date)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


# ---------------------------------------------------------------------------
# Wet bulb calculation (Stull 2011)
# ---------------------------------------------------------------------------

def wet_bulb_stull(t_dry_c: float, rh_pct: float) -> float:
    """
    Empirical wet bulb approximation (Stull 2011).

    Accurate to ±0.65°C for RH 5–99% and T −20 to 50°C.

    Reference:
        Stull, R. (2011). Wet-bulb temperature from relative humidity and
        air temperature. Journal of Applied Meteorology and Climatology,
        50(11), 2267–2269.

    Args:
        t_dry_c: Dry bulb temperature [°C]
        rh_pct:  Relative humidity [%]

    Returns:
        Wet bulb temperature [°C]
    """
    T = t_dry_c
    rh = rh_pct
    return (
        T * math.atan(0.151977 * (rh + 8.313659) ** 0.5)
        + math.atan(T + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


def rh_from_dew_point(t_dry_c: float, t_dew_c: float) -> float:
    """
    Relative humidity from dry bulb and dew point temperatures.

    Uses the Magnus formula for saturation vapour pressure.

    Args:
        t_dry_c: Dry bulb temperature [°C]
        t_dew_c: Dew point temperature [°C]

    Returns:
        Relative humidity [%]
    """
    def _sat_vp(t: float) -> float:
        return 6.112 * math.exp(17.67 * t / (t + 243.5))

    return 100.0 * _sat_vp(t_dew_c) / _sat_vp(t_dry_c)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, label: str, start: str, end: str) -> Path:
    safe_label = label.lower().replace(" ", "_")
    return cache_dir / f"{safe_label}_{start}_{end}.parquet"


def _fetch_from_api(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch from Open-Meteo and return a clean DataFrame."""
    import openmeteo_requests
    import requests_cache
    from retry_requests import retry

    session = requests_cache.CachedSession(".cache/openmeteo", expire_after=-1)
    session = retry(session, retries=5, backoff_factor=0.2)
    om = openmeteo_requests.Client(session=session)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ["temperature_2m", "wet_bulb_temperature_2m"],
        "timezone": "Australia/Brisbane",
    }

    responses = om.weather_api(_API_URL, params=params)
    response = responses[0]
    hourly = response.Hourly()

    timestamps = pd.date_range(
        start=pd.Timestamp(hourly.Time(), unit="s", tz="Australia/Brisbane"),
        end=pd.Timestamp(hourly.TimeEnd(), unit="s", tz="Australia/Brisbane"),
        freq=pd.Timedelta(seconds=hourly.Interval()),
        inclusive="left",
    )

    t_amb = hourly.Variables(0).ValuesAsNumpy()
    t_wb = hourly.Variables(1).ValuesAsNumpy()

    df = pd.DataFrame({
        "timestamp": timestamps,
        "t_amb": t_amb.astype("float64"),
        "t_wb": t_wb.astype("float64"),
    })

    # Drop any rows where either temperature is missing
    df = df.dropna(subset=["t_amb", "t_wb"]).reset_index(drop=True)

    return df
