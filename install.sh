#!/bin/bash
# install.sh — deploy systemd services
# Camera Pi: ./install.sh dashboard
# Motor Pi:  ./install.sh motor

set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

install_service() {
  local name=$1
  local src="$REPO_DIR/systemd/${name}.service"
  echo "Installing ${name}.service..."
  sudo cp "$src" /etc/systemd/system/
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
