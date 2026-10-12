"""HTML -> screenshot -> 1-bit image.

One Chromium instance is kept alive for the life of the process and guarded by
a lock; launching a browser per request costs about a second and, on a box that
is also running Home Assistant, is the difference between a frame that refreshes
and a frame that times out.

The output is a 1-bit BMP rather than a PNG on purpose. An ESP32-C3 has no
PSRAM, and ESPHome's `online_image` says outright that it "might work on
devices without PSRAM, but there is no guarantee" -- PNG decoding at 800x480 is
exactly where that runs out. A 1-bit BMP needs no decode worth the name:
800 x 480 / 8 = 48 kB of pixel data that maps straight onto the panel buffer.
"""

from __future__ import annotations

import asyncio
import io

from PIL import Image
from playwright.async_api import Browser, async_playwright

_playwright = None
_browser: Browser | None = None
_lock = asyncio.Lock()


async def _ensure_browser() -> Browser:
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        args=[
            "--disable-lcd-text",  # no subpixel fringing to threshold away
            "--force-device-scale-factor=1",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
        ]
    )
    return _browser


async def shutdown() -> None:
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


async def screenshot(html: str, width: int, height: int) -> bytes:
    """PNG bytes of `html` rendered at exactly width x height, device scale 1."""
    global _browser
    async with _lock:
        for attempt in (1, 2):
            try:
                browser = await _ensure_browser()
                page = await browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                )
                try:
                    await page.set_content(html, wait_until="load")
                    # Inline SVG and web-font-free CSS settle immediately; this
                    # only covers layout after the fonts resolve.
                    await page.wait_for_timeout(120)
                    return await page.screenshot(
                        type="png",
                        clip={"x": 0, "y": 0, "width": width, "height": height},
                    )
                finally:
                    await page.close()
            except Exception:
                # A Chromium that died between requests surfaces here. Drop it
                # and let the next attempt launch a fresh one.
                if _browser is not None:
                    try:
                        await _browser.close()
                    except Exception:
                        pass
                    _browser = None
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")


def to_monochrome(png_bytes: bytes, dither: str, threshold: int) -> Image.Image:
    """Flatten to PIL mode "1".

    `threshold` keeps text crisp: every pixel is decided independently, so glyph
    stems stay solid. `floyd` trades that for tonal range and is worth it only
    for photographic content -- on type it produces exactly the fuzzy, speckled
    look that makes e-ink dashboards hard to read across a room.
    """
    image = Image.open(io.BytesIO(png_bytes))
    # Flatten any alpha onto white first, or transparent regions threshold black.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
        canvas.alpha_composite(image)
        image = canvas
    grey = image.convert("L")

    if dither == "floyd":
        return grey.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    cut = max(0, min(255, threshold))
    return grey.point(lambda value: 255 if value >= cut else 0, mode="1")


# Quarter turns, as Pillow names them. ROTATE_90 turns counter-clockwise, so a
# panel mounted 90 degrees clockwise needs the image turned back the other way
# for its content to stand upright on the wall.
_TURNS = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def orient(image: Image.Image, rotate: int) -> Image.Image:
    """Lay a portrait composition back into the panel's native landscape buffer.

    Quarter turns on a 1-bit image are exact -- pixels are permuted, never
    resampled -- so this costs nothing in sharpness, which a threshold image
    has no margin to give away.
    """
    turn = _TURNS.get(rotate)
    return image if turn is None else image.transpose(turn)


def encode(image: Image.Image, fmt: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()
