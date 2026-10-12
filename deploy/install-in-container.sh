#!/usr/bin/env bash
# Runs INSIDE the container, as root. Pushed there by proxmox-lxc.sh, but it is
# also the whole story for a Debian box you already have: run it and you get the
# renderer on :8080.
#
#   REPO_URL=... BRANCH=... ./install-in-container.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/rlipperts/trmnl-picture-frame.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/frame-renderer
CONF_DIR=/etc/frame-renderer
BROWSERS=/opt/ms-playwright

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# fonts-dejavu-core and fonts-liberation are not optional: style.css asks for
# DejaVu Sans by name, and without it every glyph lands on different metrics
# than the layout was designed against.
apt-get install -y -qq --no-install-recommends \
    git ca-certificates curl \
    python3 python3-venv python3-pip \
    fonts-dejavu-core fonts-liberation

say "Creating the service user"
id -u frame >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin frame

say "Fetching the code"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
    rm -rf "$APP_DIR"
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
fi

say "Building the Python environment"
python3 -m venv "$APP_DIR/renderer/.venv"
"$APP_DIR/renderer/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/renderer/.venv/bin/pip" install --quiet -r "$APP_DIR/renderer/requirements.txt"

say "Installing Chromium and its system libraries (this is the slow part)"
# --with-deps is what makes this work on a bare Debian: Playwright knows which
# apt packages its Chromium build needs, which is a list nobody should maintain
# by hand. Browsers go to a shared path so the service user can read them.
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" \
    "$APP_DIR/renderer/.venv/bin/playwright" install --with-deps chromium
chmod -R a+rX "$BROWSERS"

say "Setting up configuration"
mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.yaml" ]; then
    cp "$APP_DIR/renderer/config.example.yaml" "$CONF_DIR/config.yaml"
    echo "    wrote $CONF_DIR/config.yaml from the example"
else
    echo "    kept the existing $CONF_DIR/config.yaml"
fi
if [ ! -f "$CONF_DIR/env" ]; then
    printf '# Long-lived access token: HA profile -> Security -> Long-lived access tokens.\nHA_TOKEN=\n' > "$CONF_DIR/env"
fi
chmod 640 "$CONF_DIR/env"
chgrp frame "$CONF_DIR/env" "$CONF_DIR/config.yaml"
chown -R frame:frame "$APP_DIR"

say "Installing the service"
install -m 644 "$APP_DIR/deploy/frame-renderer.service" /etc/systemd/system/frame-renderer.service
systemctl daemon-reload
systemctl enable --quiet --now frame-renderer.service

say "Waiting for it to answer"
for _ in $(seq 1 30); do
    if curl -sf -o /dev/null http://127.0.0.1:8080/healthz; then
        echo "    healthy"
        exit 0
    fi
    sleep 1
done

echo "    it did not answer in 30s -- check: journalctl -u frame-renderer -n 50" >&2
exit 1
