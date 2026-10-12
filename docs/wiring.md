# Wiring & pin mapping

## Physical connections (soldered)

ESP32-C3 Super Mini ↔ Waveshare 7.5" V2 (800×480) e-ink.

| Panel pin | Signal | ESP32-C3 GPIO |
|-----------|--------|---------------|
| CLK / SCK | SPI clock | GPIO0 |
| DIN / MOSI | SPI data | GPIO1 |
| CS | chip select | GPIO2 |
| DC | data/command | GPIO3 |
| BUSY | busy status | GPIO4 |
| RST | reset | GPIO5 |
| VCC | 3V3 | 3V3 |
| GND | ground | GND |
| — | BOOT button (manual refresh) | GPIO9 |

Power is USB. There is no battery, which is why `esphome/frame.yaml` uses no
deep sleep at all and leaves WiFi power saving off: the device stays reachable
for OTA and can refresh every five minutes without anyone caring.

## How it maps into the config

This is the whole of it, in `esphome/frame.yaml`:

```yaml
spi:
  clk_pin: GPIO0
  mosi_pin: GPIO1

display:
  - platform: epaper_spi
    model: waveshare-7.5in-v2
    cs_pin: GPIO2
    dc_pin: GPIO3
    reset_pin: GPIO5
    busy_pin:
      number: GPIO4
      inverted: true
```

`epaper_spi` is the current e-paper component; `waveshare_epaper`, which this
config used first, is frozen upstream ("deprecated and no new models will be
added to it"). It needs ESPHome 2026.9.0 or newer, which is where the UC8179
mono driver behind `waveshare-7.5in-v2` landed.

GPIO9 is the BOOT button, wired up as a `binary_sensor` that fetches a fresh
frame immediately — useful while iterating on the layout.

There is deliberately no `rotation:` here. The panel is landscape hardware; if
the picture frame hangs portrait, the renderer composes at 480×800 and turns
the bitmap into the native 800×480 buffer before sending it. See `display.rotate`
in [renderer.md](renderer.md).

## BUSY polarity — important

`busy_pin.inverted: true` is **required** for this panel.
[ESPHome's docs](https://esphome.io/components/display/epaper_spi/)
warn that the 7.5in V2 models need the BUSY line inverted to avoid permanently
damaging the display. The driver does not infer it from the model — it is yours
to set, on either component. It is not a "the screen won't refresh" convenience
setting.

This is also the reason this project no longer runs the TRMNL firmware. That
firmware drives the panel through `bb_epaper`'s `EPD_75` profile, which sets
BUSY polarity internally per panel type and exposes no equivalent flag — so on
this panel it cannot be configured into the safe state. ESPHome's driver can,
and did, from the original `frame.yaml` this build started from.
