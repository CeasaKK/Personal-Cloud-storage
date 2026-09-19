#!/usr/bin/env bash
# Install Cloudstore on the mini PC (Debian/Ubuntu). Run as root from the repo root:
#   sudo ./deploy/install.sh
# Idempotent: re-run after `git pull` to upgrade.
set -euo pipefail

PREFIX=/opt/cloudstore
DATA=/var/lib/cloudstore        # on the system SSD: metadata, staging, thumbnails
ENVFILE=/etc/cloudstore.env
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential ffmpeg smartmontools xfsprogs sqlite3 curl
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
fi

echo "==> user and directories"
id cloudstore >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin cloudstore
install -d -o cloudstore -g cloudstore -m 0750 "$DATA" "$PREFIX"
rsync -a --delete --exclude .venv --exclude node_modules --exclude demo --exclude data "$REPO"/ "$PREFIX"/src/
chown -R cloudstore:cloudstore "$PREFIX"

echo "==> python env + native SIMD kernel (compiled for THIS cpu: -march=native)"
sudo -u cloudstore python3 -m venv "$PREFIX/venv"
sudo -u cloudstore "$PREFIX/venv/bin/pip" install -q --upgrade pip
sudo -u cloudstore "$PREFIX/venv/bin/pip" install -q "$PREFIX/src/server"
sudo -u cloudstore "$PREFIX/venv/bin/python" -m cloudstore.erasure.native
sudo -u cloudstore "$PREFIX/venv/bin/python" -c "from cloudstore.erasure.backends import get_backend; print('GF backend:', get_backend().name)"

echo "==> web app"
(cd "$PREFIX/src/web" && sudo -u cloudstore npm ci --silent && sudo -u cloudstore npm run build --silent)

echo "==> config + service"
if [ ! -f "$ENVFILE" ]; then
  install -m 0640 -o root -g cloudstore "$REPO/deploy/cloudstore.env.example" "$ENVFILE"
fi
install -m 0644 "$REPO/deploy/cloudstore.service" /etc/systemd/system/cloudstore.service
systemctl daemon-reload
systemctl enable cloudstore >/dev/null

cat <<EOF

Installed. Next steps (see docs/DEPLOY.md):
  1. Prepare and mount the six data disks at /srv/cloud/disks/d1..d6, give them to the
     cloudstore user (chown cloudstore: /srv/cloud/disks/d*), then register each one:
       sudo -u cloudstore env \$(cat $ENVFILE | xargs) $PREFIX/venv/bin/cloudstore disk add /srv/cloud/disks/d1
  2. Set the password:
       sudo -u cloudstore env \$(cat $ENVFILE | xargs) $PREFIX/venv/bin/cloudstore set-password
  3. Start:            sudo systemctl start cloudstore
  4. Expose on tailnet: sudo tailscale serve --bg 8000
EOF
