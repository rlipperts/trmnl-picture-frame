"""Configuration loading.

The YAML file is re-read on every request so that editing it and refreshing the
browser is the whole edit loop -- no container restart.
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path("/config/config.yaml")


class SafeArea(BaseModel):
    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0


class Display(BaseModel):
    # The panel's own geometry, as the driver sees it. The 7.5" V2 is a
    # landscape 800x480 device no matter which way up it is screwed to a wall.
    width: int = 800
    height: int = 480
    # How far clockwise the panel is mounted from that native landscape. At 90
    # or 270 the layout is composed portrait and the device image is rotated
    # back to 800x480 on the way out, so the ESP32 keeps blitting a native
    # buffer and never learns the frame is sideways.
    rotate: Literal[0, 90, 180, 270] = 0
    safe_area: SafeArea = Field(default_factory=SafeArea)
    dither: Literal["threshold", "floyd"] = "threshold"
    threshold: int = 128

    @property
    def turned(self) -> bool:
        return self.rotate in (90, 270)

    @property
    def render_width(self) -> int:
        """Viewport width the HTML is laid out in -- what a viewer sees."""
        return self.height if self.turned else self.width

    @property
    def render_height(self) -> int:
        return self.width if self.turned else self.height


class Location(BaseModel):
    latitude: float
    longitude: float
    timezone: str = "Europe/Berlin"
    label: str = ""


class Window(BaseModel):
    name: str
    start: time
    end: time


class Outings(BaseModel):
    # Any time you are outside and want the rain verdict for it -- the ride in,
    # the walk to the canteen, the ride home. The panel shows whichever is next.
    windows: list[Window]
    rain_jacket_pop: int = 30
    heavy_rain_mm: float = 1.0


class Forecast(BaseModel):
    hours: int = 14


class HomeAssistant(BaseModel):
    base_url: str
    token: str | None = None
    timeout_seconds: float = 8.0


class StatusRow(BaseModel):
    entity: str
    label: str
    icon: str = "dot"
    on_states: list[str] = Field(default_factory=lambda: ["on"])
    on_text: str = "ON"
    off_text: str = "off"


class Cache(BaseModel):
    weather_seconds: int = 600
    states_seconds: int = 20


class Config(BaseModel):
    display: Display = Field(default_factory=Display)
    location: Location
    outings: Outings
    forecast: Forecast = Field(default_factory=Forecast)
    home_assistant: HomeAssistant
    status: list[StatusRow] = Field(default_factory=list)
    cache: Cache = Field(default_factory=Cache)

    @property
    def ha_token(self) -> str | None:
        """Env var wins over the file, so the token need not be committed."""
        return os.environ.get("HA_TOKEN") or self.home_assistant.token


def config_path() -> Path:
    return Path(os.environ.get("FRAME_CONFIG", DEFAULT_CONFIG_PATH))


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Copy config.example.yaml there and edit it."
        )
    with path.open("rb") as handle:
        raw = yaml.safe_load(handle) or {}
    return Config.model_validate(raw)
