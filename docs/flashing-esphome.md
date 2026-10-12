# Flashing and running the ESPHome firmware

The device runs stock ESPHome. There is no custom firmware, no patch and no
submodule involved in this path -- `esphome/frame.yaml` is the whole thing.

## One-time setup

```sh
cd esphome
cp secrets.example.yaml secrets.yaml
openssl rand -base64 32        # paste the result as api_key in secrets.yaml
$EDITOR secrets.yaml
```

ESPHome itself needs Python. A throwaway venv keeps it off your system Python:

```sh
python -m venv .venv
source .venv/bin/activate.fish   # bash/zsh: source .venv/bin/activate
pip install esphome
```

Or install nothing and prefix every `esphome` command below with `uvx`:

```sh
uvx --python 3.12 esphome run frame.yaml
```

Pinning the Python version matters on a distribution that already ships the
newest one: the PlatformIO toolchain underneath ESPHome lags a release behind.

Also set `frame_url` in `frame.yaml` to wherever the renderer is listening.

## First flash (over USB)

The ESP32-C3 Super Mini exposes the C3's native USB, so no serial adapter is
needed -- but the bootloader has to be entered by hand the first time:

> **hold BOOT, tap RST, release BOOT**

Then:

```sh
esphome run frame.yaml
```

Pick the USB device when prompted (typically `/dev/ttyACM0`).

If the upload succeeds and the output then stops at `Starting log output`, the
chip is still sitting in the bootloader: the automatic reset does not reliably
start the new firmware over the C3's native USB. **Tap RST once**, without
BOOT. The log viewer reports `Serial port closed!` as the port re-enumerates --
that is expected, and `esphome logs frame.yaml` reattaches.

After the first successful flash, every later change goes over the air and the
cable can stay in the drawer:

```sh
esphome run frame.yaml --device <device-ip-or-name>.local
```

## Watching it work

```sh
esphome logs frame.yaml
```

A healthy cycle where the picture changed logs:

```
[online_image] Updating image from http://.../frame.bmp
[frame] Frame changed, refreshing panel
[epaper_spi] Display update took 4xxx ms
```

A cycle where it did not:

```
[online_image] Server returned HTTP 304 (Not Modified). Download skipped.
```

The second is the common case and is the point: the panel is only repainted
when the image actually differs. See "Refreshing only on change" in
[renderer.md](renderer.md). If you *never* see a 304, the renderer is emitting
a new picture every render — check that nothing on the frame carries a
minute-resolution timestamp.

## Adding it to Home Assistant

ESPHome devices announce themselves over mDNS. Settings -> Devices & Services
should offer the device for adoption; paste the `api_key` from `secrets.yaml`
when asked for the encryption key. That gives you a "Refresh frame" button and a
"Restart" button in HA, plus WiFi signal and uptime for diagnostics.

Note that Home Assistant is *not* in the data path. The renderer reads HA over
its REST API and the device reads the renderer -- so the frame keeps working
through an HA restart, showing whatever the renderer last managed to build.

## Things that go wrong

**Panel never refreshes, logs stall on BUSY.** Check `busy_pin.inverted` is
still `true`. The 7.5in V2 panels need it, and ESPHome's docs warn that getting
it wrong risks damaging the display permanently -- this is not a cosmetic
setting.

**Panel stalls on BUSY right after reset.** `epaper_spi` pulses the reset line
low for 10 ms by default, which this panel's datasheet is comfortable with; the
`waveshare_epaper` config this replaced needed an explicit `reset_duration: 2ms`
to get under its own 200 ms default. If a particular driver-board revision turns
out to be fussy, `reset_duration` is still accepted on the display block.

**Panel does not update and the logs only ever say 304.** That is the change
gate doing its job, but it also means a genuinely stuck picture stays stuck.
Press the "Repaint panel" button (or GPIO9 for a fresh fetch) to force pixels
onto the glass without waiting for the content to change.

**`Decode error` or a reboot loop during download.** The C3 has no PSRAM, so the
download buffer is the tightest allocation in the build. Confirm the renderer is
serving a 1-bit BMP and not a PNG:

```sh
curl -s -D - -o /dev/null http://<renderer>:8080/frame.bmp \
    | grep -i content-type                                     # image/bmp
curl -s http://<renderer>:8080/frame.bmp | wc -c               # 48062
```

48062 bytes is the expected size: a 62-byte BMP header plus 800 x 480 / 8 bytes
of pixels. Anything much larger means the format or bit depth is wrong.

**Image appears inverted.** Swap the colour arguments in the display lambda:

```yaml
it.image(0, 0, id(frame_image), Color::WHITE, Color::BLACK);
```

**Nothing downloads at all.** `http_request` is a required dependency of the
`online_image` platform; if you trimmed it out of the config, downloads fail
silently at startup.
