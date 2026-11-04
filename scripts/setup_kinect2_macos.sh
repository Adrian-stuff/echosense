#!/usr/bin/env bash
# Build libfreenect2 and the Echo-Sense Kinect v2 bridge on macOS (Apple Silicon or Intel).
# Needs: Homebrew, a Kinect v2 with the Kinect Adapter for Windows (USB 3.0 + 12 V supply).
#
#   scripts/setup_kinect2_macos.sh
#   ~/.local/freenect2/bin/Protonect          # optional: libfreenect2's own viewer, to check the hardware
#   .venv/bin/echosense run --source kinect2
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="$HOME/.local/freenect2"
WORK="${TMPDIR:-/tmp}/echosense-libfreenect2"

brew install cmake pkg-config libusb jpeg-turbo glfw

rm -rf "$WORK" && git clone --depth 1 https://github.com/OpenKinect/libfreenect2.git "$WORK/src"
JT="$(brew --prefix jpeg-turbo)"
# CMAKE_POLICY_VERSION_MINIMUM: libfreenect2 predates CMake 4, which refuses its old minimum version
cmake -S "$WORK/src" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DENABLE_OPENCL=OFF -DENABLE_CUDA=OFF -DENABLE_OPENGL=ON \
  -DBUILD_EXAMPLES=ON -DTurboJPEG_INCLUDE_DIRS="$JT/include" -DTurboJPEG_LIBRARIES="$JT/lib/libturbojpeg.dylib"
cmake --build "$WORK/build" -j"$(sysctl -n hw.ncpu)"
cmake --install "$WORK/build"
cp "$WORK/build/bin/Protonect" "$PREFIX/bin/" 2>/dev/null || true

cmake -S "$ROOT/native/kinect2_bridge" -B "$ROOT/native/build" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -Dfreenect2_DIR="$PREFIX/lib/cmake/freenect2"
cmake --build "$ROOT/native/build"
echo
echo "Bridge built: $ROOT/native/build/echosense-kinect2"
echo "Plug in the Kinect v2, then:  .venv/bin/echosense run --source kinect2"
