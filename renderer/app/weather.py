"""Bright Sky client -- DWD open data (MOSMIX forecast + station observations).

Bright Sky (https://brightsky.dev) is a free JSON front end over the Deutscher
Wetterdienst open-data service. No API key, no account. It resolves the nearest
DWD station to the configured coordinates itself.

Field notes learned from the live API, because they shape the code below:
  * `precipitation_probability` is populated on forecast records and null on
    observation records.
  * `relative_humidity` is the mirror image: present on observations, null on
    forecasts. Neither is read any more -- the frame reports air temperature
    and leaves how it feels to the person reading it.
  * /current_weather does not carry a plain `wind_speed`. It reports rolling
    windows instead -- `wind_speed_10`, `_30`, `_60` -- and the same for gusts.
  * Default units are degrees C, km/h and mm.
"""

from __future__ import annotations

import time as clock
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Config, Window

API_BASE = "https://api.brightsky.dev"
ATTRIBUTION = "DWD / Bright Sky"

# Readable headline for each icon value. Bright Sky's own `condition` field is
# a precipitation vocabulary -- its most common value is the unhelpful "dry" --
# so the sky description comes from `icon` instead, and `condition` is only
# consulted when something is actually falling.
CONDITION_TEXT = {
    "clear-day": "Clear",
    "clear-night": "Clear",
    "partly-cloudy-day": "Partly cloudy",
    "partly-cloudy-night": "Partly cloudy",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "wind": "Windy",
    "rain": "Rain",
    "sleet": "Sleet",
    "snow": "Snow",
    "hail": "Hail",
    "thunderstorm": "Thunderstorm",
}

# Bright Sky's icon vocabulary mapped onto the SVG macros in icons.html.j2.
ICON_MAP = {
    "clear-day": "sun",
    "clear-night": "moon",
    "partly-cloudy-day": "partly-cloudy",
    "partly-cloudy-night": "partly-cloudy-night",
    "cloudy": "cloud",
    "fog": "fog",
    "wind": "wind",
    "rain": "rain",
    "sleet": "sleet",
    "snow": "snow",
    "hail": "hail",
    "thunderstorm": "thunderstorm",
}

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, producer):
    now = clock.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    return value


@dataclass
class Hour:
    when: datetime
    temperature: float | None
    wind_kmh: float | None
    gust_kmh: float | None
    precipitation_mm: float | None
    precipitation_probability: int | None
    cloud_cover: int | None
    condition: str | None
    icon: str

    @property
    def label(self) -> str:
        return self.when.strftime("%H")


@dataclass
class Current:
    temperature: float | None
    wind_kmh: float | None
    gust_kmh: float | None
    condition: str | None
    icon: str
    station: str


def _icon(raw: str | None) -> str:
    return ICON_MAP.get(raw or "", "cloud")


def _condition_text(icon_raw: str | None, condition: str | None) -> str:
    """Sky description, preferring the icon; precipitation overrides it."""
    if condition and condition not in {"dry", None}:
        return CONDITION_TEXT.get(condition, condition.replace("-", " ").title())
    return CONDITION_TEXT.get(icon_raw or "", "Cloudy")


def _window(record: dict, field: str) -> float | None:
    """Pick a rolling-window observation, preferring the 30-minute mean.

    The 10-minute figure is twitchy enough that the panel would disagree with
    itself between refreshes; the 60-minute one lags a changing wind.
    """
    for suffix in ("_30", "_60", "_10"):
        value = record.get(f"{field}{suffix}")
        if value is not None:
            return value
    return record.get(field)


def _hour_from_record(record: dict, tz: ZoneInfo) -> Hour:
    when = datetime.fromisoformat(record["timestamp"]).astimezone(tz)
    return Hour(
        when=when,
        temperature=record.get("temperature"),
        wind_kmh=record.get("wind_speed"),
        gust_kmh=record.get("wind_gust_speed"),
        precipitation_mm=record.get("precipitation"),
        precipitation_probability=record.get("precipitation_probability"),
        cloud_cover=record.get("cloud_cover"),
        condition=record.get("condition"),
        icon=_icon(record.get("icon")),
    )


def fetch_hours(config: Config, hours: int, start: datetime | None = None) -> list[Hour]:
    """Hourly records from `start` (default: the current hour) forwards."""
    tz = ZoneInfo(config.location.timezone)
    begin = (start or datetime.now(tz)).replace(minute=0, second=0, microsecond=0)
    end = begin + timedelta(hours=max(hours - 1, 0))
    key = f"hours:{begin.isoformat()}:{hours}"

    def produce() -> list[Hour]:
        params = {
            "lat": config.location.latitude,
            "lon": config.location.longitude,
            "date": begin.strftime("%Y-%m-%dT%H:%M"),
            "last_date": end.strftime("%Y-%m-%dT%H:%M"),
            "tz": config.location.timezone,
        }
        response = httpx.get(f"{API_BASE}/weather", params=params, timeout=15.0)
        response.raise_for_status()
        records = response.json().get("weather", [])
        return [_hour_from_record(record, tz) for record in records]

    return _cached(key, config.cache.weather_seconds, produce)


def fetch_current(config: Config) -> Current:
    """Latest station observation, with the forecast hour as a stand-in."""

    def produce() -> Current:
        params = {
            "lat": config.location.latitude,
            "lon": config.location.longitude,
            "tz": config.location.timezone,
        }
        response = httpx.get(f"{API_BASE}/current_weather", params=params, timeout=15.0)
        response.raise_for_status()
        payload = response.json()
        record = payload.get("weather") or {}
        sources = payload.get("sources") or [{}]
        return Current(
            temperature=record.get("temperature"),
            wind_kmh=_window(record, "wind_speed"),
            gust_kmh=_window(record, "wind_gust_speed"),
            condition=_condition_text(record.get("icon"), record.get("condition")),
            icon=_icon(record.get("icon")),
            station=(sources[0].get("station_name") or "").title(),
        )

    return _cached("current", config.cache.weather_seconds, produce)


def hours_in_window(hours: list[Hour], start, end) -> list[Hour]:
    """Subset of `hours` whose clock time falls inside [start, end]."""
    return [hour for hour in hours if start <= hour.when.time() <= end]


def next_outing(config: Config) -> tuple[Window, list[Hour], bool]:
    """The next configured window, and the forecast hours covering it.

    Past the last window of the day, roll to the first one of tomorrow: at
    21:00 the useful question is about the ride in, not one that has already
    happened. The bool says whether that roll occurred, so the panel can label
    it rather than implying the window is minutes away.

    The window's hours are fetched for the window itself rather than sliced out
    of the strip's list. The strip covers `forecast.hours` from now, which at
    18:30 reaches only as far as tomorrow 07:00 -- slicing would have computed
    the morning verdict from the first hour of a three-hour ride and called it
    the whole picture.
    """
    tz = ZoneInfo(config.location.timezone)
    now = datetime.now(tz)
    windows = sorted(config.outings.windows, key=lambda window: window.start)

    chosen, rolled = windows[0], True
    for window in windows:
        if now.time() <= window.end:
            chosen, rolled = window, False
            break

    day = now.date() + timedelta(days=1) if rolled else now.date()
    begin = datetime.combine(day, chosen.start, tzinfo=tz)
    end = datetime.combine(day, chosen.end, tzinfo=tz)
    # Standing in the hallway at 08:15 of an 07:00-09:00 ride, the hour already
    # behind you cannot be rained on any more. Start from the current hour when
    # the window is underway, so the verdict covers what is left of it.
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if begin < current_hour <= end:
        begin = current_hour
    # Inclusive of both ends: an 07:00-09:00 ride wants 07, 08 and 09.
    span = int((end - begin).total_seconds() // 3600) + 1

    hours = fetch_hours(config, span, start=begin)
    return chosen, hours_in_window(hours, chosen.start, chosen.end), rolled
