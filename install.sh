#!/usr/bin/env bash
# TuxLens installer for a git checkout (no packaging needed).
#
#   ./install.sh            install for the current user (asks for sudo once for the camera device)
#   ./install.sh --uninstall
#
# What it does:
#   * system: tuxlens-loopback.service creates /dev/video10 "TuxLens" at boot (v4l2loopback)
#   * user:   tuxlens.service runs the daemon from this checkout; `tuxlens` and `tuxlens-panel`
#             are linked into ~/.local/bin
set -euo pipefail

APPDIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN="$HOME/.local/bin"
SUDO="${SUDO:-sudo}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

check_deps() {
  local missing=()
  python3 -c 'import aiohttp' 2>/dev/null || missing+=("python-aiohttp")
  python3 -c 'import av' 2>/dev/null || missing+=("python-av")
  python3 -c 'import numpy' 2>/dev/null || missing+=("python-numpy")
  python3 -c 'import cairo' 2>/dev/null || missing+=("python-cairo")
  python3 -c 'import gi; gi.require_version("Gtk", "4.0")' 2>/dev/null || missing+=("gtk4 python-gobject")
  command -v openssl >/dev/null || missing+=("openssl")
  command -v v4l2loopback-ctl >/dev/null || missing+=("v4l2loopback-utils")
  modinfo v4l2loopback >/dev/null 2>&1 || missing+=("v4l2loopback-dkms")
  if ((${#missing[@]})); then
    say "Missing dependencies: ${missing[*]}"
    echo "On Arch: sudo pacman -S --needed ${missing[*]} gtk4-layer-shell qrencode"
    exit 1
  fi
}

install() {
  check_deps
  say "Camera device (system service, needs sudo)…"
  $SUDO install -Dm644 "$APPDIR/packaging/tuxlens-loopback.service" /etc/systemd/system/tuxlens-loopback.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now tuxlens-loopback.service

  say "Daemon (user service)…"
  mkdir -p "$USER_UNITS" "$BIN"
  sed "s|@APPDIR@|$APPDIR|g" "$APPDIR/packaging/tuxlens.service.in" > "$USER_UNITS/tuxlens.service"
  ln -sf "$APPDIR/bin/tuxlens" "$BIN/tuxlens"
  ln -sf "$APPDIR/panel/tuxlens-panel" "$BIN/tuxlens-panel"
  systemctl --user daemon-reload
  systemctl --user enable --now tuxlens.service

  say "Done. Pair your phone with:  tuxlens pair"
  echo "Waybar module example: see README (\"image#tuxlens\")."
}

uninstall() {
  systemctl --user disable --now tuxlens.service 2>/dev/null || true
  rm -f "$USER_UNITS/tuxlens.service" "$BIN/tuxlens" "$BIN/tuxlens-panel"
  systemctl --user daemon-reload
  $SUDO systemctl disable --now tuxlens-loopback.service 2>/dev/null || true
  $SUDO rm -f /etc/systemd/system/tuxlens-loopback.service
  $SUDO systemctl daemon-reload
  say "Removed. Settings and certificates stay in ~/.config/tuxlens (delete it to forget the pairing)."
}

case "${1:-}" in
  --uninstall) uninstall ;;
  "") install ;;
  *) echo "usage: $0 [--uninstall]" >&2; exit 2 ;;
esac
