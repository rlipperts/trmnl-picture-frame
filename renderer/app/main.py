"""FastAPI app.

Endpoint map:

  GET /             browser preview -- same document, plus a dashed panel edge
                    and shaded mat margins so you can see what the picture
                    frame is covering. This is the design loop.
  GET /frame.html   the exact document that gets screenshotted, no guides.
  GET /frame.png    what the device will actually show, after the 1-bit
                    flattening, viewable in a browser.
  GET /frame.bmp    what the device fetches. 1-bit BMP, ~48 kB. Carries an
                    ETag over the encoded bytes and answers a matching
                    If-None-Match with 304, so the panel only repaints when
                    the picture actually changed. See REVALIDATE below.
  GET /calibrate    ruler page for measuring the frame's hidden margins.
  GET /calibrate.bmp  the same, for the device to display.
  GET /debug.json   the view model plus raw source values.
  GET /healthz      liveness.
"""

from __future__ import annotations

import hashlib
import logging
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import ha, shot, weather
from .config import Config, load_config

log = logging.getLogger("frame")

APP_DIR = Path(__file__).parent
TEMPLATE_DIR = APP_DIR / "templates"
CSS_PATH = APP_DIR / "static" / "style.css"

# Height band, in the plot's 0-100 coordinate space, that the temperature
# polyline is allowed to occupy. It sits above the tallest rain bar so the two
# series stay legible where they overlap.
PLOT_TOP = 10.0
PLOT_BOTTOM = 72.0
# Smallest temperature span to normalise against: without a floor, a day that
# varies by half a degree would render as a dramatic mountain range.
MIN_TEMP_SPAN = 2.0

jinja = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await shot.shutdown()


app = FastAPI(title="E-ink frame renderer", lifespan=lifespan)

NO_STORE = {"Cache-Control": "no-store, max-age=0"}

# Images say "no-cache", not "no-store": the device is *supposed* to keep the
# last copy and revalidate it. That is the whole conditional-request mechanism
# that lets the panel skip a repaint, and "no-store" would tell a well-behaved
# client to throw the copy away.
REVALIDATE = {"Cache-Control": "no-cache, max-age=0"}


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _round(value: float | None, digits: int = 0) -> Any:
    if value is None:
        return None
    return round(value, digits) if digits else int(round(value))


def _safe_now(timezone: str) -> datetime:
    """Local time, falling back to the system clock on a bad timezone key.

    The error page renders through here. Using ZoneInfo directly there meant a
    misspelt `timezone:` in config.yaml took out the very page whose job is to
    tell you what is wrong with config.yaml -- a 500 instead of a panel you can
    read from the sofa.
    """
    try:
        return datetime.now(ZoneInfo(timezone))
    except Exception:
        return datetime.now()


def _stamp(timezone: str) -> str:
    """The "updated" line, deliberately truncated to the hour.

    Anything finer makes every render byte-unique, which defeats the ETag on
    /frame.bmp and puts the panel through a full refresh every five minutes
    for nothing. At hour resolution the stamp still answers "is the renderer
    still alive", and it bounds the forced repaints at 24 a day -- which also
    satisfies Waveshare's "refresh at least once every 24 hours".
    """
    return _safe_now(timezone).strftime("%a %d %b %H:00")


def _num(value: float | None) -> str:
    """For anything that goes straight into the template as text.

    A DWD station can be briefly missing a field; an em dash on the panel is
    honest, whereas Jinja happily rendering the word "None" at 72 px is not.
    """
    return "--" if value is None else str(int(round(value)))


def build_view(config: Config) -> dict:
    """Assemble everything the template needs. No layout decisions live here."""
    current = weather.fetch_current(config)
    hours = weather.fetch_hours(config, config.forecast.hours)
    status = ha.fetch_status(config)
    window, window_hours, tomorrow = weather.next_outing(config)
    window_keys = {hour.when for hour in window_hours}

    # -- rain verdict for the next time you are outside -----------------------
    pops = [h.precipitation_probability or 0 for h in window_hours]
    rains = [h.precipitation_mm or 0.0 for h in window_hours]
    gusts = [h.gust_kmh or 0.0 for h in window_hours]

    max_pop = max(pops) if pops else 0
    total_rain = sum(rains)
    max_gust = max(gusts) if gusts else 0.0

    chips = []
    wet = max_pop >= config.outings.rain_jacket_pop
    heavy = total_rain >= config.outings.heavy_rain_mm
    chips.append(
        {
            "icon": "rain" if wet else "sun",
            "text": ("Rain jacket" if wet else "No jacket"),
            "sub": f"{max_pop}% · {total_rain:.1f} mm" if wet else f"{max_pop}%",
            "alert": wet,
        }
    )
    if heavy:
        chips.append(
            {"icon": "droplet", "text": "Proper rain", "sub": f"{total_rain:.1f} mm",
             "alert": True}
        )
    if max_gust >= 40:
        chips.append(
            {"icon": "gust", "text": "Windy", "sub": f"gusts {_round(max_gust)} km/h",
             "alert": True}
        )

    # -- hourly strip ---------------------------------------------------------
    strip_hours = []
    strip_temps: list[float | None] = []
    for hour in hours:
        pop = hour.precipitation_probability or 0
        mm = hour.precipitation_mm or 0.0
        strip_hours.append(
            {
                # Not "pop": Jinja resolves attribute access on a dict to its
                # methods first, so {{ hour.pop }} renders dict.pop itself.
                "rain_pct": pop,
                "label": hour.label,
                "mm": round(mm, 1),
                "bar_pct": max(0, min(100, pop)),
                # A number over every column is noise when the whole day reads
                # 1%. Label only the hours worth reading.
                "show_pct": pop >= 10,
                "wet": mm >= 0.1,
                "in_window": hour.when in window_keys,
            }
        )
        strip_temps.append(hour.temperature)

    known = [value for value in strip_temps if value is not None]
    temp_points = ""
    temp_min = temp_max = None
    if known:
        temp_min, temp_max = min(known), max(known)
        span = max(temp_max - temp_min, MIN_TEMP_SPAN)
        points = []
        for index, value in enumerate(strip_temps):
            if value is None:
                continue
            ratio = (value - temp_min) / span
            y = PLOT_BOTTOM - ratio * (PLOT_BOTTOM - PLOT_TOP)
            points.append(f"{index + 0.5:.2f},{y:.2f}")
        temp_points = " ".join(points)

    return {
        "now": {
            "temperature": _num(current.temperature),
            "icon": current.icon,
            "condition": current.condition or "",
            "wind": _num(current.wind_kmh),
            "gust": _round(current.gust_kmh),
            "station": current.station,
        },
        "status": status,
        "outing": {
            "title": f"Tomorrow · {window.name}" if tomorrow else window.name,
            "span": f"{window.start:%H:%M}-{window.end:%H:%M}",
            "chips": chips,
        },
        "strip": {
            "hours": strip_hours,
            "temp_points": temp_points,
            "temp_min": _round(temp_min),
            "temp_max": _round(temp_max),
        },
        "footer": {
            "location": config.location.label or "",
            "attribution": weather.ATTRIBUTION,
            "updated": _stamp(config.location.timezone),
        },
    }


def render_frame_html(preview: bool) -> str:
    """The dashboard, or an error page carrying the reason it could not render."""
    config = load_config()
    if config.display.calibrate:
        # The device only knows one URL. Swapping the picture here is what lets
        # the ruler reach the panel without re-flashing a different frame_url.
        return render_calibrate_html(preview)
    try:
        view = build_view(config)
    except Exception as exc:
        log.exception("view build failed")
        return jinja.get_template("error.html.j2").render(
            display=config.display,
            inline_css=_css(),
            preview=preview,
            title="Frame error",
            summary=f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc(limit=4),
            updated=_stamp(config.location.timezone),
        )
    return jinja.get_template("frame.html.j2").render(
        display=config.display,
        inline_css=_css(),
        preview=preview,
        title="Frame",
        view=view,
    )


def render_calibrate_html(preview: bool) -> str:
    config = load_config()
    return jinja.get_template("calibrate.html.j2").render(
        display=config.display,
        inline_css=_css(),
        preview=preview,
        title="Calibrate",
        rings=list(range(0, 61, 10)),
    )


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: a comma-separated list, entries possibly weak."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


async def _image(request: Request, html: str, fmt: str, media_type: str) -> Response:
    """Render, encode, and answer conditionally.

    The render happens either way -- the only way to know whether the picture
    changed is to draw it. What the ETag saves is not server work but a panel
    refresh: the device keeps the previous image and skips repainting, which is
    both four seconds of flashing nobody asked for and one of the million
    refreshes the panel is specified for.

    The BMP is turned into the panel's native orientation; the PNG is not. A
    browser is not screwed to a wall, so /frame.png should show the picture the
    way a person standing in front of the frame sees it.
    """
    config = load_config()
    display = config.display
    png = await shot.screenshot(html, display.render_width, display.render_height)
    mono = shot.to_monochrome(png, display.dither, display.threshold)
    if fmt == "BMP":
        mono = shot.orient(mono, display.rotate)
    payload = shot.encode(mono, fmt)
    # Over the encoded bytes, so any pixel difference is a new tag and nothing
    # else is. blake2s is plenty here: this guards against accidental
    # collisions between two frames, not against an adversary.
    etag = '"%s"' % hashlib.blake2s(payload, digest_size=16).hexdigest()

    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag, **REVALIDATE})

    return Response(
        content=payload,
        media_type=media_type,
        headers={"ETag": etag, **REVALIDATE},
    )


@app.get("/", response_class=HTMLResponse)
async def preview() -> Response:
    return HTMLResponse(render_frame_html(preview=True), headers=NO_STORE)


@app.get("/frame.html", response_class=HTMLResponse)
async def frame_html() -> Response:
    return HTMLResponse(render_frame_html(preview=False), headers=NO_STORE)


@app.get("/frame.png")
async def frame_png(request: Request) -> Response:
    return await _image(request, render_frame_html(preview=False), "PNG", "image/png")


@app.get("/frame.bmp")
async def frame_bmp(request: Request) -> Response:
    return await _image(request, render_frame_html(preview=False), "BMP", "image/bmp")


@app.get("/calibrate", response_class=HTMLResponse)
async def calibrate() -> Response:
    return HTMLResponse(render_calibrate_html(preview=False), headers=NO_STORE)


@app.get("/calibrate.bmp")
async def calibrate_bmp(request: Request) -> Response:
    return await _image(
        request, render_calibrate_html(preview=False), "BMP", "image/bmp"
    )


@app.get("/debug.json")
async def debug() -> Response:
    config = load_config()
    try:
        view = build_view(config)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}",
             "traceback": traceback.format_exc(limit=6)},
            status_code=500,
            headers=NO_STORE,
        )

    def plain(value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [plain(item) for item in value]
        return value

    return JSONResponse(
        {
            "ok": True,
            "ha_base_url": config.home_assistant.base_url,
            "ha_token_present": bool(config.ha_token),
            "station_resolved_from": {
                "latitude": config.location.latitude,
                "longitude": config.location.longitude,
            },
            "view": plain(view),
        },
        headers=NO_STORE,
    )


@app.get("/healthz")
async def healthz() -> Response:
    return JSONResponse({"ok": True}, headers=NO_STORE)
