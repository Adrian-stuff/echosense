#!/usr/bin/env bash
# Run Echo-Sense with no internet connection.
#
#   ./run.sh                      start the device software + web console (http://localhost:8080)
#   ./run.sh --source sim         any `echosense run` option is passed through
#   ./run.sh info|bench|perf ...  other echosense commands work too
#   ./run.sh --prepare            (needs internet, once) save every dependency as a wheel in
#                                 ./wheels so a fresh checkout can be set up offline later
#   ./run.sh --check              only check that everything is installed, then exit
#
# Nothing in Echo-Sense itself uses the network: the web console's 3D library is vendored in
# echosense/web/vendor and speech uses the local `say` / espeak. The console is served from this
# machine, so it works with Wi-Fi off. Only installing Python packages needs the internet, and
# this script never does that: it uses ./.venv, or builds it from ./wheels or uv's local cache.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
WHEELS="$ROOT/wheels"
DEPS=(numpy aiohttp sounddevice psutil)

say_() { printf '  %s\n' "$*"; }
die() { printf '\n  error: %s\n\n' "$*" >&2; exit 1; }

deps_ok() {
  [ -x "$PY" ] && "$PY" -c "import numpy, aiohttp, sounddevice, psutil, echosense" >/dev/null 2>&1
}

prepare() {
  command -v uv >/dev/null || die "--prepare needs uv (https://docs.astral.sh/uv/)"
  mkdir -p "$WHEELS"
  say_ "downloading wheels for: ${DEPS[*]} pytest setuptools wheel  ->  $WHEELS"
  # Wheels for the Python in .venv (or 3.10) on this machine's platform.
  local pyver="3.10"
  [ -x "$PY" ] && pyver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  uv tool run --python "$pyver" --from pip pip download --only-binary=:all: -d "$WHEELS" \
    "${DEPS[@]}" pytest setuptools wheel
  # Make sure the Python interpreter itself is cached by uv for an offline `uv venv`.
  uv python install "$pyver" >/dev/null 2>&1 || true
  say_ "done. ./run.sh will now set up offline from ./wheels if .venv is missing."
}

install_offline() {
  say_ "setting up .venv without internet ..."
  export UV_OFFLINE=1 PIP_NO_INDEX=1
  if [ ! -x "$PY" ]; then
    if command -v uv >/dev/null; then
      uv venv --python 3.10 "$VENV" >/dev/null 2>&1 || uv venv "$VENV" >/dev/null \
        || die "uv could not create .venv offline (no cached Python). Run ./run.sh --prepare while online."
    else
      command -v python3 >/dev/null || die "no python3 found"
      python3 -m venv "$VENV" || die "could not create .venv"
    fi
  fi
  local find=()
  [ -d "$WHEELS" ] && find=(--find-links "$WHEELS")
  if command -v uv >/dev/null; then
    uv pip install --python "$PY" --offline ${find[@]+"${find[@]}"} -e "$ROOT" \
      || die "dependencies are not available offline. Run ./run.sh --prepare once while online."
  else
    [ -d "$WHEELS" ] || die "no ./wheels folder. Run ./run.sh --prepare once while online."
    "$PY" -m pip install --no-index ${find[@]+"${find[@]}"} -e "$ROOT" \
      || die "could not install from ./wheels"
  fi
}

case "${1:-}" in
  --prepare) prepare; exit 0 ;;
  -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

cd "$ROOT"
deps_ok || install_offline
deps_ok || die "Echo-Sense still cannot be imported from .venv"

if [ "${1:-}" = "--check" ]; then
  say_ "ok: .venv has everything Echo-Sense needs; it can run offline."
  exec "$PY" -m echosense info
fi

# Keep anything that might phone home quiet (pip/uv/HF-style tools); the app itself never does.
export UV_OFFLINE=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

# --config/--profile belong before the subcommand; everything else defaults to `run`.
global=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config|--profile) [ $# -ge 2 ] || die "$1 needs a value"; global+=("$1" "$2"); shift 2 ;;
    --config=*|--profile=*) global+=("$1"); shift ;;
    *) break ;;
  esac
done
case "${1:-}" in
  run|bench|protocol|perf|info|eval-sensing|eval-map|train-objects) ;;
  *) set -- run "$@" ;;
esac
exec "$PY" -m echosense ${global[@]+"${global[@]}"} "$@"
