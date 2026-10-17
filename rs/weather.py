"""Current conditions from Open-Meteo (no API key)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import net

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, as documented by Open-Meteo.
WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


@dataclass
class Weather:
    temp: float
    feels_like: Optional[float]
    high: Optional[float]
    low: Optional[float]
    code: int
    humidity: Optional[float]
    wind: Optional[float]
    is_day: bool
    utc_offset_seconds: int
    units: str          # "F" or "C"
    fetched_at: float

    @property
    def condition(self) -> str:
        return WMO.get(self.code, "")

    @property
    def wind_unit(self) -> str:
        return "mph" if self.units == "F" else "km/h"

    def summary(self) -> str:
        """One line for the panel, e.g. '54°F  Partly cloudy  ·  H 61° L 47°'."""
        parts = [f"{round(self.temp)}°{self.units}"]
        if self.condition:
            parts.append(self.condition)
        line = "  ".join(parts)
        if self.high is not None and self.low is not None:
            line += f"  ·  H {round(self.high)}°  L {round(self.low)}°"
        return line


def parse(data: dict, units: str) -> Weather:
    cur = data["current"]
    daily = data.get("daily") or {}

    def first(key):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    return Weather(
        temp=float(cur["temperature_2m"]),
        feels_like=cur.get("apparent_temperature"),
        high=first("temperature_2m_max"),
        low=first("temperature_2m_min"),
        code=int(cur["weather_code"]) if cur.get("weather_code") is not None else -1,  # -1: no condition
        humidity=cur.get("relative_humidity_2m"),
        wind=cur.get("wind_speed_10m"),
        is_day=bool(cur.get("is_day", 1)),
        utc_offset_seconds=int(data.get("utc_offset_seconds") or 0),
        units=units,
        fetched_at=time.time(),
    )


def fetch(lat: float, lon: float, units: str = "F") -> Weather:
    params = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "timezone": "auto",
    }
    if units == "F":
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    return parse(net.get_json(FORECAST_URL, params), units)
