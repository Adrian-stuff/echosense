#!/usr/bin/env bash
# Install Echo-Sense on an Orange Pi (arm64, Ubuntu 22.04 / Debian 12 / Armbian).
#
# The Docker virtual Orange Pi is built with this same script, so the virtual
# device and the real board get identical packages, driver build and layout.
#
#   sudo deploy/install_orangepi.sh                 # full install on the board (+ systemd service)
#   sudo deploy/install_orangepi.sh --deps          # system packages + libfreenect2 only
#   sudo deploy/install_orangepi.sh --app           # bridge + Python app only (deps already present)
#        options: --no-systemd   --user NAME (default: orangepi)   --opencl (Mali OpenCL pipeline)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/echosense
RUN_USER=orangepi
DO_DEPS=1
DO_APP=1
SYSTEMD=1
OPENCL=OFF
LIBFREENECT2_REF="${LIBFREENECT2_REF:-master}"

while [ $# -gt 0 ]; do
  case "$1" in
    --deps) DO_APP=0 ;;
    --app) DO_DEPS=0 ;;
    --no-systemd) SYSTEMD=0 ;;
    --user) RUN_USER="$2"; shift ;;
    --opencl) OPENCL=ON ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
  shift
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [ "$DO_DEPS" = 1 ]; then
  log "System packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential cmake pkg-config \
    python3 python3-venv python3-dev \
    libusb-1.0-0-dev libturbojpeg0-dev libjpeg-dev \
    libportaudio2 alsa-utils espeak-ng \
    python3-libgpiod gpiod udev
  if [ "$OPENCL" = ON ]; then apt-get install -y --no-install-recommends ocl-icd-opencl-dev; fi

  log "libfreenect2 ($LIBFREENECT2_REF) - Kinect v2 driver"
  tmp="$(mktemp -d)"
  git clone --depth 1 --branch "$LIBFREENECT2_REF" https://github.com/OpenKinect/libfreenect2.git "$tmp/libfreenect2"
  cmake -S "$tmp/libfreenect2" -B "$tmp/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DBUILD_EXAMPLES=OFF -DENABLE_OPENGL=OFF -DENABLE_OPENCL="$OPENCL" -DENABLE_CUDA=OFF -DENABLE_VAAPI=OFF \
    -DENABLE_TEGRAJPEG=OFF -DBUILD_OPENNI2_DRIVER=OFF
  cmake --build "$tmp/build" -j"$(nproc)"
  cmake --install "$tmp/build"
  install -m 644 "$tmp/libfreenect2/platform/linux/udev/90-kinect2.rules" /etc/udev/rules.d/90-kinect2.rules
  ldconfig
  rm -rf "$tmp"
fi

if [ "$DO_APP" = 1 ]; then
  log "Kinect v2 bridge"
  cmake -S "$SRC/native/kinect2_bridge" -B "$SRC/native/build" -DCMAKE_BUILD_TYPE=Release \
    -Dfreenect2_DIR=/usr/local/lib/cmake/freenect2
  cmake --build "$SRC/native/build" -j"$(nproc)"
  install -m 755 "$SRC/native/build/echosense-kinect2" /usr/local/bin/echosense-kinect2

  log "Python application"
  mkdir -p "$PREFIX"
  python3 -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install --no-cache-dir --upgrade pip
  "$PREFIX/venv/bin/pip" install --no-cache-dir "$SRC[dev]"

  if id "$RUN_USER" >/dev/null 2>&1; then
    usermod -aG audio,plugdev "$RUN_USER" || true
    getent group gpio >/dev/null && usermod -aG gpio "$RUN_USER" || true
  fi

  if [ "$SYSTEMD" = 1 ] && command -v systemctl >/dev/null; then
    log "systemd service (starts Echo-Sense at power-on)"
    sed "s/@USER@/$RUN_USER/" "$SRC/deploy/echosense.service" > /etc/systemd/system/echosense.service
    systemctl daemon-reload
    systemctl enable echosense.service
    echo "Start now with: sudo systemctl start echosense   (logs: journalctl -u echosense -f)"
  fi
fi
log "Done"
