# The renderer

A small service that turns Home Assistant state and DWD weather into one
800x480 one-bit image. It exists so that the layout is HTML and CSS rather than
draw calls: the whole point of the rewrite is that nothing is positioned by
coordinate any more.

## Running it

```sh
cd renderer
mkdir -p config
cp config.example.yaml config/config.yaml
$EDITOR config/config.yaml        # coordinates, outing windows, HA entities

echo 'HA_TOKEN=paste-long-lived-token-here' > .env
docker compose up -d --build
```

The Home Assistant token comes from your HA profile page -> Security ->
Long-lived access tokens. It is read from the environment rather than
`config.yaml` so the config file stays safe to commit or paste.

`config/config.yaml` is re-read on **every request**. Editing it and refreshing
the browser is the entire edit loop -- no restart, no rebuild.

## Running it on Proxmox

One script on the node builds an LXC and installs the service, no Docker in the
picture. See [proxmox-lxc.md](proxmox-lxc.md).

## Running it locally, without Docker

For layout work, skip the container: the image is built on the official
Playwright base and runs to about 2 GB, which is a lot to rebuild for a CSS
tweak. Chromium runs perfectly well straight off the host.

```sh
cd renderer
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt
.venv/bin/playwright install chromium        # ~150 MB into ~/.cache/ms-playwright

mkdir -p config && cp config.example.yaml config/config.yaml
FRAME_CONFIG=config/config.yaml .venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8080 --reload
```

`--reload` restarts on Python edits; templates and CSS are read per request, so
those need no restart at all.

Playwright warns that Arch is "not officially supported" and downloads its
ubuntu20.04 build. That build runs fine -- no extra system packages needed.

**Home Assistant is optional here.** With no `HA_TOKEN` set, every status row
renders as `?` and the rest of the frame is unaffected: `app/ha.py` returns
`None` for an unreachable entity rather than raising. Weather needs no key at
all, so a bare checkout renders real DWD data immediately. Set `HA_TOKEN` in
the environment when you want the HOME panel populated.

## Endpoints

| URL | What it is |
|-----|-----------|
| `/` | Browser preview. The same document, plus a dashed panel edge and shaded mat margins so you can see what the picture frame is covering. |
| `/frame.html` | The exact document that gets screenshotted, with no guides. |
| `/frame.png` | What the device will show, after the one-bit flattening. Viewable in a browser. |
| `/frame.bmp` | What the device fetches. 1-bit BMP, 48062 bytes, with an `ETag`. |
| `/calibrate` | Ruler page, for measuring what the frame hides. |
| `/calibrate.bmp` | The same, for the device to display. |
| `/debug.json` | The assembled view model plus whether the HA token was found. |
| `/healthz` | Liveness. |

The useful habit is to keep `/` open in one tab and `/frame.png` in another. The
first is where you design; the second is where you find out that a hairline rule
you liked disappeared into the threshold.

## Refreshing only on change

`/frame.bmp`, `/frame.png` and `/calibrate.bmp` hash the encoded bytes into an
`ETag` and answer a matching `If-None-Match` with `304 Not Modified`. ESPHome's
`online_image` sends that header on its own and exposes the outcome to the
device config as `cached`, which `frame.yaml` uses to skip repainting the panel
when nothing changed.

This costs the renderer nothing and saves the panel a great deal. Drawing the
picture is the only way to know whether it changed, so the render happens
either way -- what is avoided is a four-second full refresh, visible across the
room, to paint pixels that are already there. Overnight, most polls are 304s.

Check it by hand:

```sh
etag=$(curl -s -D - -o /dev/null http://<host>:8080/frame.bmp \
       | tr -d '\r' | awk '/^[Ee][Tt]ag:/ {print $2}')
curl -s -D - -o /dev/null -H "If-None-Match: $etag" \
     http://<host>:8080/frame.bmp | head -1        # HTTP/1.1 304 Not Modified
```

Use `GET` with `-o /dev/null`, not `curl -sI`: the routes are declared `GET`
only, so a `HEAD` request answers `405`. The device only ever sends `GET`,
which is why this has never mattered in practice.

**The consequence for the layout:** anything on the frame that changes every
render defeats this entirely. That is why the footer stamp is truncated to the
hour (`_stamp()` in `app/main.py`) rather than showing minutes -- it still
answers "is the renderer alive", while bounding the forced repaints at 24 a day.
Which incidentally satisfies Waveshare's other instruction, to refresh the panel
at least once every 24 hours. If you add a seconds-resolution clock, a per-render
random element, or anything animated, expect the panel to go back to repainting
on every single poll.

## Hanging it portrait

The panel is landscape hardware -- 800×480 is what the driver drives, whichever
way the frame is screwed to the wall. `display.rotate` says how far clockwise
the panel is mounted from that:

```yaml
display:
  width: 800
  height: 480
  rotate: 90      # 0, 90, 180 or 270
```

At 90 or 270 the layout is composed at 480×800 and the finished bitmap is
turned back into the panel's native buffer on the way out. Two consequences:

- **The device knows nothing about it.** `frame.yaml` has no `rotation:`, and
  the ESP32 blits a native 800×480 image exactly as before. Rotating on the C3
  would mean transforming 384000 pixels per refresh to arrive at the same
  picture.
- **The turn is free.** A quarter turn of a 1-bit image permutes pixels rather
  than resampling them, so nothing is softened -- which matters, because a
  thresholded image has no softness to spare.

`/frame.png` and `/` stay portrait: a browser is not screwed to a wall, so the
preview shows what a person standing in front of the frame sees. Only
`/frame.bmp` and `/calibrate.bmp` -- the two the device fetches -- come back
rotated.

**If the picture is upside down on the wall, swap 90 for 270.** Which one is
right depends on which way the ribbon cable leaves the panel, and that is
faster to settle by looking than by reasoning.

The layout is not the landscape one squeezed. `style.css` ends with a
`@media (max-aspect-ratio: 1 / 1)` block that dissolves the two-column header,
stacks the panels, moves the rain verdict up under the temperature, and gives
the hourly strip the height that the wide layout spends on a second column.

## Measuring what the picture frame covers

This is the part that used to be painful and is now a two-minute job.

1. Put the ruler on the panel: set `display.calibrate: true` in `config.yaml`
   and press the device's BOOT button, which fetches at once instead of waiting
   out the five-minute poll. While the switch is on, `/frame.bmp` serves the
   ruler instead of the dashboard, so nothing on the device changes.
2. The page draws nested rings every 10 px, each labelled in the top-left and
   bottom-right corners. Read off, per edge, the innermost ring the mat still
   hides.
3. Put those four numbers into `safe_area` in `config/config.yaml`.
4. Set `calibrate` back to `false` and press BOOT again. The layout reflows
   inside the visible area -- it does not shift content off the panel, because
   everything is laid out with grid and flex against that inset rather than
   placed at absolute positions.

If a whole corner is missing rather than an edge strip, the panel is sitting
skewed in the frame and no amount of inset will fix it.

## Editing the layout

- `app/templates/frame.html.j2` -- structure.
- `app/static/style.css` -- all of the visual design.
- `app/templates/icons.html.j2` -- the icon set, as inline SVG.
- `app/main.py`, `build_view()` -- what data reaches the template. It makes no
  layout decisions; it produces numbers and strings.

Two rules the stylesheet is built around, both worth keeping:

**No greys.** The image is flattened with a hard threshold, so a mid-grey is a
coin flip -- it can land black on one refresh and white on the next. Depth comes
from inversion (a black block with white text) and from rule weight. Where a
fill is genuinely wanted, it is a hatch pattern drawn at 90 degrees so it stays
on the pixel grid.

**No coordinates.** Grid and flex place everything. This is what makes the
safe-area inset a single number per edge instead of an offset applied to every
element.

Text is rendered with antialiasing switched off (`-webkit-font-smoothing:
none`), because grey edge pixels are exactly what makes small type look furry
once thresholded. The one large temperature figure opts back in, since glyphs
that big have enough mass to threshold cleanly and look much better for it.

## The outing windows

`outings.windows` is a list of the times you are actually outside:

```yaml
outings:
  windows:
    - { name: "Ride in",   start: "07:00", end: "09:00" }
    - { name: "Canteen",   start: "11:00", end: "12:00" }
    - { name: "Ride home", start: "16:00", end: "18:00" }
```

The panel shows whichever window is next and names it. Add, remove or rename
them freely -- they are sorted by start time, so the order in the file does not
matter. Once the last one of the day has passed the panel rolls to the first of
tomorrow and says so ("Tomorrow · Ride in") rather than implying it is minutes
away.

Two details that are easy to get wrong and are handled in `next_outing()`:

- A window in progress is clipped to the hours that remain. At 08:15 of an
  07:00-09:00 ride the verdict covers 08 and 09; the hour you already rode
  cannot be rained on any more.
- The window's forecast hours are fetched for the window itself, not sliced out
  of the hourly strip. The strip only runs `forecast.hours` from now, so at
  18:30 it reaches tomorrow 07:00 and no further -- slicing would have judged a
  three-hour morning ride by its first hour.

The verdict itself is the worst hour in the window, not the average: peak
precipitation probability against `rain_jacket_pop`, and total accumulation
against `heavy_rain_mm` for the "Proper rain" upgrade.

## Where the data comes from

**Weather: DWD, via [Bright Sky](https://brightsky.dev).** A free JSON front end
over Deutscher Wetterdienst open data -- no API key, no account. It resolves the
nearest DWD station to your coordinates itself. Hourly MOSMIX forecast for the
strip and the rain verdict; station observations for the "now" panel.

Two field quirks shape `app/weather.py`, and both were found by querying the
live API rather than reading the docs:

- `precipitation_probability` is populated on forecast records and null on
  observations. `relative_humidity` is the exact mirror image. Neither is read
  any more, but the asymmetry is worth knowing before reaching for either.
- `/current_weather` has no plain `wind_speed`. It reports rolling windows:
  `wind_speed_10`, `_30`, `_60`, and the same for gusts. The code prefers the
  30-minute mean -- the 10-minute figure is twitchy enough that the panel would
  disagree with itself between refreshes.

**Air temperature, not a comfort index.** The frame reports what the
thermometer says and stops there. An earlier version derived an apparent
temperature (Australian formula, plus your own riding headwind added to the
forecast wind) and turned it into a clothing hint. It was removed on purpose:
how cold 8 degrees and a headwind feels is a judgement its owner already has,
and a number that claims otherwise is a confident guess dressed up as data.
What stays is the one verdict a forecast genuinely settles better than a
person looking out of a window -- whether it is going to rain during the ride.

**Home Assistant.** One `GET /api/states/<entity>` per configured row. Entities
reporting `unavailable` or `unknown` render as `?` with a marker on the leading
edge, rather than as "closed" -- showing a window as shut in exactly the case
where the data is missing is the one failure worth designing against.

## When something breaks

A data-source failure renders an error page *to the panel*, carrying the
exception. A frame on the wall saying `httpx.ConnectError` is far more useful
than one silently showing yesterday's weather as though it were today's.

`/debug.json` returns the assembled view model and confirms whether the HA token
was picked up.
