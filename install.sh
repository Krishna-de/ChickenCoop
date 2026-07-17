#!/bin/bash
# install.sh — deploy systemd services
# Camera Pi: sudo ./install.sh dashboard
# Motor Pi:  sudo ./install.sh motor

set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
# When run under sudo, SUDO_USER is the real login user — the unit must run as
# that user (not root) so it keeps GPIO/video group access and $HOME paths.
RUN_USER="${SUDO_USER:-$USER}"

install_service() {
  local name=$1
  local src="$REPO_DIR/systemd/${name}.service"
  local dst="/etc/systemd/system/${name}.service"

  echo "Installing ${name}.service  (user=${RUN_USER}, dir=${REPO_DIR})"
  # Fill the template so the unit matches THIS machine's user and repo path.
  sed -e "s|__DIR__|${REPO_DIR}|g" -e "s|__USER__|${RUN_USER}|g" "$src" \
    | sudo tee "$dst" > /dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable "$name"
  sudo systemctl restart "$name"
  sudo systemctl status "$name" --no-pager
}

case "${1:-}" in
  dashboard) install_service "coop-dashboard" ;;
  motor)     install_service "motor-server" ;;
  both)      install_service "coop-dashboard"; install_service "motor-server" ;;
  *) echo "Usage: $0 [dashboard|motor|both]"; exit 1 ;;
esac
