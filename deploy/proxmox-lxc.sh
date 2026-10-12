#!/usr/bin/env bash
# Create a Debian LXC on a Proxmox host and install the frame renderer in it.
#
# Run on the PROXMOX HOST (node shell in the web UI, or ssh to the node), as
# root. It asks four questions, all with working defaults, then does the rest.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/rlipperts/trmnl-picture-frame/main/deploy/proxmox-lxc.sh)"
#
# Read it before running it. That advice applies to every script anyone ever
# tells you to pipe into a shell, this one included.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rlipperts/trmnl-picture-frame.git}"
BRANCH="${BRANCH:-main}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/rlipperts/trmnl-picture-frame/$BRANCH}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

ask() { # ask VARNAME "prompt" "default"
    local __var="$1" __prompt="$2" __default="$3" __reply
    read -r -p "$__prompt [$__default]: " __reply </dev/tty || true
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

# -- preflight ---------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run this as root on the Proxmox host."
command -v pct >/dev/null || die "No 'pct' command -- this is not a Proxmox host."

# Storage that can hold a container's root filesystem. 'local-lvm' on a stock
# install; picking the first one that advertises 'rootdir' beats guessing.
mapfile -t ROOT_STORES < <(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 {print $1}')
[ "${#ROOT_STORES[@]}" -gt 0 ] || die "No storage on this node can hold a container rootfs."

# Storage for the downloaded Debian template. Usually 'local'.
mapfile -t TMPL_STORES < <(pvesm status --content vztmpl 2>/dev/null | awk 'NR>1 {print $1}')
[ "${#TMPL_STORES[@]}" -gt 0 ] || die "No storage on this node can hold container templates."

# Network bridges, as configured on the node. 'vmbr0' is the one that reaches
# your LAN on a stock install -- the same switch your HAOS is plugged into.
mapfile -t BRIDGES < <(awk '/^(auto|iface) vmbr[0-9]+/ {print $2}' /etc/network/interfaces 2>/dev/null | sort -u)
[ "${#BRIDGES[@]}" -gt 0 ] && DEFAULT_BRIDGE="${BRIDGES[0]}" || DEFAULT_BRIDGE="vmbr0"

DEFAULT_CTID="$(pvesh get /cluster/nextid 2>/dev/null || echo 200)"

# -- questions ---------------------------------------------------------------

bold "E-ink frame renderer -- Proxmox LXC installer"
cat <<'INTRO'

Creates an unprivileged Debian 12 container running the renderer on port 8080.
It needs about 1 GB of RAM and 8 GB of disk, most of which is Chromium.

INTRO

ask CTID     "Container ID"                     "$DEFAULT_CTID"
ask HOSTNAME "Hostname"                         "frame-renderer"
ask STORAGE  "Storage for the container disk"   "${ROOT_STORES[0]}"
ask BRIDGE   "Network bridge"                   "$DEFAULT_BRIDGE"

DISK_GB="${DISK_GB:-8}"
CORES="${CORES:-2}"
RAM_MB="${RAM_MB:-1024}"
TMPL_STORE="${TMPL_STORE:-${TMPL_STORES[0]}}"

pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

# -- template ----------------------------------------------------------------

say "Finding a Debian 12 template"
pveam update >/dev/null 2>&1 || warn "Could not refresh the template list; using what is cached."
TEMPLATE="$(pveam available --section system | awk '{print $2}' | grep '^debian-12-standard' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || die "No debian-12-standard template offered by this node."

if ! pveam list "$TMPL_STORE" 2>/dev/null | grep -q "$TEMPLATE"; then
    say "Downloading $TEMPLATE (about 130 MB)"
    pveam download "$TMPL_STORE" "$TEMPLATE"
fi

# -- create ------------------------------------------------------------------

say "Creating container $CTID"
# nesting=1 is the one flag that matters here. Chromium sandboxes each tab with
# user namespaces, and an unprivileged container cannot nest those without it --
# the browser then refuses to start, and the usual workaround is to disable its
# sandbox, which is a worse trade on a machine that also runs your home.
pct create "$CTID" "$TMPL_STORE:vztmpl/$TEMPLATE" \
    --hostname "$HOSTNAME" \
    --cores "$CORES" \
    --memory "$RAM_MB" \
    --swap 512 \
    --rootfs "$STORAGE:$DISK_GB" \
    --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
    --features nesting=1 \
    --unprivileged 1 \
    --onboot 1 \
    --description "E-ink picture frame renderer -- $REPO_URL" \
    >/dev/null

say "Starting it"
pct start "$CTID"

say "Waiting for the network"
IP=""
for _ in $(seq 1 60); do
    IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')" || true
    [ -n "$IP" ] && break
    sleep 2
done
[ -n "$IP" ] || die "Container $CTID never got an IP address. Check the bridge ($BRIDGE)."
echo "    $IP"

# -- install -----------------------------------------------------------------

say "Installing the renderer inside the container"
INNER="$(mktemp)"
trap 'rm -f "$INNER"' EXIT
# Use the sibling file when this script was run from a checkout, and fetch it
# when it was piped in from the internet -- where BASH_SOURCE is "bash" rather
# than a path, so there is nothing to sit next to.
SELF_DIR=""
SELF="${BASH_SOURCE[0]:-}"
[ -n "$SELF" ] && [ -f "$SELF" ] && SELF_DIR="$(cd "$(dirname "$SELF")" && pwd)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/install-in-container.sh" ]; then
    cp "$SELF_DIR/install-in-container.sh" "$INNER"
else
    curl -fsSL "$RAW_BASE/deploy/install-in-container.sh" -o "$INNER"
fi
pct push "$CTID" "$INNER" /root/install-in-container.sh --perms 755
pct exec "$CTID" -- env REPO_URL="$REPO_URL" BRANCH="$BRANCH" /root/install-in-container.sh

# -- done --------------------------------------------------------------------

cat <<DONE

$(bold "Done.") The renderer is at http://$IP:8080/

Open that in a browser: it is the layout, live. /frame.png shows what the panel
will render after the one-bit flattening.

Two things it does not know yet:

  1. Where you are, and what to show.
       pct exec $CTID -- nano /etc/frame-renderer/config.yaml
     Coordinates, the outing windows, and your Home Assistant entities. The
     file is re-read on every request, so a browser refresh is the edit loop.

  2. Your Home Assistant token, for the entity rows. Without it they render
     as "?" and everything else works.
       pct exec $CTID -- nano /etc/frame-renderer/env
       pct exec $CTID -- systemctl restart frame-renderer

Then point the device at it -- in esphome/frame.yaml:

    frame_url: "http://$IP:8080/frame.bmp"

Useful later:

    pct exec $CTID -- journalctl -u frame-renderer -f     # logs
    pct exec $CTID -- /root/install-in-container.sh       # update to latest main

DONE
