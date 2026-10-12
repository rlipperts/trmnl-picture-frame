# Running the renderer on Proxmox

The renderer is one Python process driving a headless Chromium. That fits an
LXC container directly, with no Docker in between -- which matters on Proxmox,
where running Docker on the node itself is discouraged and the usual advice is
a container or VM anyway. Doing it as an LXC means one layer instead of three.

## The one-liner

On the **Proxmox host** -- the node's shell, either through the web UI
(`Datacenter -> your node -> Shell`) or over ssh -- as root:

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/rlipperts/trmnl-picture-frame/main/deploy/proxmox-lxc.sh)"
```

Read [the script](../deploy/proxmox-lxc.sh) first. That goes for anything
anyone tells you to pipe into a root shell, this included -- it is about 150
lines and does nothing clever.

It asks four questions, all with defaults that are right on a stock install,
then takes a few minutes (most of it downloading Chromium) and prints the URL.

## What it is asking you

**Container ID** -- Proxmox numbers every container and VM. The default is the
next free number. It is only a label; nothing depends on the value.

**Hostname** -- the container's name, in the UI and on the network.

**Storage for the container disk** -- which pool the container's filesystem
lives on. On a stock install that is `local-lvm`. The script lists what your
node actually offers and defaults to the first one that can hold a container.

**Network bridge** -- the virtual switch the container plugs into. `vmbr0` on a
stock install: the one that hands out normal LAN addresses from your router,
the same one your Home Assistant sits on. The container takes a DHCP lease, so
give it a reservation in your router if you would rather its address never
moved -- the device config points at it by IP.

Sizing is not asked about, since there is one right answer: 2 cores, 1 GB RAM,
8 GB disk. Chromium is the whole of that. Override with `CORES=`, `RAM_MB=`,
`DISK_GB=` in the environment if you disagree.

## What it builds

An **unprivileged** Debian 12 container with **`nesting=1`**.

Those two together are the only part worth understanding. Unprivileged means
root inside the container is not root on your Proxmox host, which is what you
want for something that runs a browser against the internet. But Chromium
sandboxes its own tabs using user namespaces, and an unprivileged container
cannot nest namespaces unless nesting is enabled -- without it Chromium simply
refuses to start, and every guide on the internet tells you to pass
`--no-sandbox`. That trade (keep the container restricted, unlock the browser's
own sandbox) is the wrong way round on a machine that also runs your house, so
the script sets the flag instead.

Inside, it installs Python, DejaVu and Liberation fonts, the pinned
requirements, and Chromium via `playwright install --with-deps` -- which knows
its own apt dependencies, a list nobody should maintain by hand.

The fonts are not cosmetic. `style.css` asks for DejaVu Sans by name; without
it every glyph lands on different metrics and the layout stops matching what
you designed in a browser.

Then it installs [`frame-renderer.service`](../deploy/frame-renderer.service),
enables it, and waits for `/healthz` to answer.

## Where things live

```
/opt/frame-renderer/          the checkout -- a git clone, updated by pulling
/opt/ms-playwright/           Chromium, shared and read-only to the service
/etc/frame-renderer/config.yaml   your settings; outside the checkout, so a
                                  pull never touches them
/etc/frame-renderer/env           HA_TOKEN, mode 640, root:frame
```

The service runs as an unprivileged `frame` user, with `ProtectSystem=strict`
and a private `/tmp`. `HOME` points into that private tmp, because Chromium
scatters caches and profile directories across `$HOME` and none of it should
outlive the process.

## Afterwards

```sh
pct exec <ctid> -- nano /etc/frame-renderer/config.yaml   # coordinates, windows, entities
pct exec <ctid> -- nano /etc/frame-renderer/env           # HA_TOKEN=...
pct exec <ctid> -- systemctl restart frame-renderer       # only needed for the token
```

`config.yaml` is re-read on every request, so editing it and refreshing the
browser is the whole loop -- no restart. The token is read at startup, so that
one needs the restart.

Then point the device at the container, in `esphome/frame.yaml`:

```yaml
frame_url: "http://<container-ip>:8080/frame.bmp"
```

## Updating

```sh
pct exec <ctid> -- /root/install-in-container.sh
```

Pulls the latest `main`, reinstalls requirements, restarts the service. Your
config and token are untouched -- that is why they live outside the checkout.

## When it goes wrong

```sh
pct exec <ctid> -- journalctl -u frame-renderer -n 50
pct exec <ctid> -- systemctl status frame-renderer
```

**`Target page, context or browser has been closed`, or Chromium failing to
launch at all.** Almost always nesting. Check the container has it:

```sh
pct config <ctid> | grep features        # want: features: nesting=1
pct set <ctid> --features nesting=1      # if missing, then reboot the container
```

**The frame shows an error page about Home Assistant.** The renderer draws its
own error page rather than going blank, which is the point. Read it -- it
carries the exception. A bad token or an unreachable HA shows up as `?` rows
instead, which is not an error.

**Everything works from the node but not from the device.** The container took
a DHCP lease; confirm it still has the address in `frame_url` with
`pct exec <ctid> -- hostname -I`.

## Not using Proxmox?

[`install-in-container.sh`](../deploy/install-in-container.sh) is the whole
install and has nothing Proxmox-specific in it. Run it as root on any Debian
box and you get the same service on :8080. `renderer/compose.yaml` is still
there if you would rather have Docker.
