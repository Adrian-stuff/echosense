#!/usr/bin/env bash
# Install Echo-Sense on an Orange Pi One (Allwinner H3, 32-bit armhf, 512 MB) with a Kinect v1.
# Tested OS target: Armbian / Debian 12 "bookworm" armhf. Everything comes from apt (no compiling
# on the board): numpy, aiohttp, psutil, libfreenect + its Python module.
#
#   sudo deploy/install_orangepi_one.sh            # install + systemd service
#   sudo deploy/install_orangepi_one.sh --no-systemd
#
# The Kinect v2 cannot be used on this board: it needs USB 3.0 and far more CPU for depth decoding.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/echosense
RUN_USER=orangepi
SYSTEMD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-systemd) SYSTEMD=0 ;;
    --user) RUN_USER="$2"; shift ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
  shift
done
log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "System packages (all prebuilt for armhf)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl python3 python3-venv python3-pip python3-setuptools \
  python3-numpy python3-aiohttp python3-psutil python3-cffi \
  libportaudio2 alsa-utils espeak-ng \
  freenect python3-freenect udev

log "Kinect v1 USB permissions"
cat > /etc/udev/rules.d/66-kinect-v1.rules <<'RULES'
# Microsoft Kinect for Xbox 360: motor, audio, camera
SUBSYSTEM=="usb", ATTR{idVendor}=="045e", ATTR{idProduct}=="02b0", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="045e", ATTR{idProduct}=="02ad", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="045e", ATTR{idProduct}=="02ae", MODE="0666"
RULES

log "Python application"
mkdir -p "$PREFIX"
python3 -m venv --system-site-packages "$PREFIX/venv"   # reuse apt's numpy/aiohttp/psutil/freenect
"$PREFIX/venv/bin/pip" install --no-cache-dir --no-deps sounddevice
"$PREFIX/venv/bin/pip" install --no-cache-dir --no-deps --no-build-isolation "$SRC"

if id "$RUN_USER" >/dev/null 2>&1; then
  usermod -aG audio,plugdev "$RUN_USER" || true
fi

if [ "$SYSTEMD" = 1 ] && command -v systemctl >/dev/null; then
  log "systemd service"
  sed -e "s/@USER@/$RUN_USER/" -e "s/ECHOSENSE_PROFILE=orangepi5/ECHOSENSE_PROFILE=orangepi_one/" \
    "$SRC/deploy/echosense.service" > /etc/systemd/system/echosense.service
  systemctl daemon-reload
  systemctl enable echosense.service
  echo "Start now with: sudo systemctl start echosense"
fi
log "Done. Check it keeps up:  $PREFIX/venv/bin/echosense --profile orangepi_one perf --source kinect1 --minutes 5"
