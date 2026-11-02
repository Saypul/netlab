#!/usr/bin/env bash
# NetLab installer for Ubuntu / Debian servers (run as root).
#
# Private repository (token with read access to Saypul/netlab):
#   curl -fsSL -H "Authorization: token <TOKEN>" \
#     https://raw.githubusercontent.com/Saypul/netlab/main/install.sh | sudo NETLAB_GITHUB_TOKEN=<TOKEN> bash
# From a checkout:   sudo ./install.sh
#
# Env: NETLAB_DIR (default /opt/netlab), NETLAB_PORT (8765), NETLAB_REPO (Saypul/netlab),
#      NETLAB_GITHUB_TOKEN, NETLAB_REF (a tag or branch; default: the newest release tag, else main)
set -euo pipefail

DIR=${NETLAB_DIR:-/opt/netlab}
PORT=${NETLAB_PORT:-8765}
REPO=${NETLAB_REPO:-Saypul/netlab}
TOKEN=${NETLAB_GITHUB_TOKEN:-}

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\nnetlab install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
. /etc/os-release 2>/dev/null || true
case "${ID:-}${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) die "needs Ubuntu or Debian (found ${PRETTY_NAME:-unknown})";; esac
[ "$(uname -m)" = x86_64 ] || echo "warning: $(uname -m) host; x86 router images will be emulated and slow"

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl python3 python3-venv iproute2 iptables dnsmasq-base qemu-system-x86 qemu-utils
if ! command -v docker >/dev/null; then
  apt-get install -y -q docker.io
fi
systemctl enable --now docker

git_auth=()
if [ -n "$TOKEN" ]; then
  git_auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)")
fi

say "Getting NetLab into $DIR"
if [ -d "$DIR/.git" ]; then
  echo "$DIR already has NetLab; keeping it (use 'netlab update' for new releases)"
else
  [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && die "$DIR exists and is not empty"
  GIT_TERMINAL_PROMPT=0 git "${git_auth[@]}" clone -q "https://github.com/$REPO.git" "$DIR" \
    || die "cannot clone $REPO; for a private repository set NETLAB_GITHUB_TOKEN"
  ref=${NETLAB_REF:-$(git -C "$DIR" tag --list 'v*' --sort=-v:refname | head -1)}
  [ -n "$ref" ] && git -C "$DIR" checkout -q --detach "$ref"
fi
mkdir -p "$DIR/data"
if [ -n "$TOKEN" ]; then  # for 'netlab update'; data/ is never committed
  install -m 600 /dev/null "$DIR/data/github_token"
  printf '%s\n' "$TOKEN" > "$DIR/data/github_token"
fi

say "Python environment"
[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

say "Service and 'netlab' command"
cat > /etc/systemd/system/netlab.service <<EOF
[Unit]
Description=NetLab network simulator
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh
Environment=NETLAB_PORT=$PORT
Restart=on-failure
# only stop the web server; QEMU VMs it started keep running across restarts and updates
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
cat > /usr/local/bin/netlab <<EOF
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/cli.py" "\$@"
EOF
chmod 755 /usr/local/bin/netlab "$DIR/run.sh"
systemctl daemon-reload
systemctl enable -q netlab
systemctl restart netlab

say "Starting"
for _ in $(seq 30); do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 1
done
curl -fs -o /dev/null "http://127.0.0.1:$PORT/" || die "NetLab did not start; see: journalctl -u netlab -n 50"

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "NetLab $(cat "$DIR/VERSION") is running: http://${ip:-<server>}:$PORT/"
if [ -f "$DIR/data/initial_password" ]; then
  echo "First sign-in: user admin, password $(cat "$DIR/data/initial_password")"
fi
[ -e /dev/kvm ] || echo "note: /dev/kvm is missing, so VMs run without hardware acceleration (slow)"
echo "Manage it with: netlab update | netlab status | netlab logs | netlab user passwd <user>"
