#!/usr/bin/env bash
# Build a standalone, single-file `grid` binary for the CURRENT OS+arch with Nuitka.
#
# Nuitka compiles for the OS+arch it runs on — it CANNOT cross-compile. The release
# workflow (.github/workflows/release.yml) runs this on each Linux runner (x86_64 +
# arm64) and renames the output to grid-linux-<arch>. macOS isn't shipped as a binary
# (it's SIGKILL'd unless notarized — see packaging/README.md), but this script still
# builds a macOS binary locally for dev/testing. Reproduce one target:
#
#   packaging/build_binary.sh            # -> dist/grid for this machine
#   GRID_BUILD_PYTHON=python3.12 packaging/build_binary.sh
#
# Output: dist/grid . Self-contained — no Python/uv needed at runtime.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

resolve_python() { "$1" -c 'import os, sys; print(os.path.realpath(sys.executable))'; }

pick_python() {
  if [ -n "${GRID_BUILD_PYTHON:-}" ]; then
    if command -v "$GRID_BUILD_PYTHON" >/dev/null 2>&1; then resolve_python "$(command -v "$GRID_BUILD_PYTHON")"; return; fi
    echo "ERROR: GRID_BUILD_PYTHON set but not runnable: $GRID_BUILD_PYTHON" >&2; exit 1
  fi
  # Homebrew paths first on macOS, then PATH, then uv-managed interpreters.
  for c in \
    /opt/homebrew/opt/python@3.11/bin/python3.11 \
    /opt/homebrew/opt/python@3.12/bin/python3.12 \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    python3.11 python3.12 python3.13 python3
  do
    if command -v "$c" >/dev/null 2>&1; then resolve_python "$(command -v "$c")"; return; fi
  done
  if command -v uv >/dev/null 2>&1; then
    for v in 3.11 3.12 3.13; do
      p="$(uv python find "$v" 2>/dev/null || true)"
      [ -n "$p" ] && [ -x "$p" ] && { resolve_python "$p"; return; }
    done
  fi
  echo "ERROR: need Python 3.11/3.12/3.13 (brew install python@3.12 or uv python install 3.12)" >&2; exit 1
}

BUILD_PY="$(pick_python)"
BUILD_VERSION="$("$BUILD_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$BUILD_VERSION" in
  3.11|3.12|3.13) ;;
  *) echo "ERROR: build needs Python 3.11/3.12/3.13; got $BUILD_VERSION at $BUILD_PY" >&2; exit 1 ;;
esac

case "$(uname -s)" in Darwin) OS_SUFFIX=macos ;; Linux) OS_SUFFIX=linux ;; *) OS_SUFFIX=unknown ;; esac

BUILD_VENV="$REPO_ROOT/.venv-build"
if [ -x "$BUILD_VENV/bin/python" ]; then
  VENV_VERSION="$("$BUILD_VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  case "$VENV_VERSION" in 3.11|3.12|3.13) ;; *) echo ">>> Removing stale $BUILD_VENV"; rm -rf "$BUILD_VENV" ;; esac
fi
[ -x "$BUILD_VENV/bin/python" ] || { echo ">>> Creating build venv ($BUILD_PY)"; "$BUILD_PY" -m venv "$BUILD_VENV"; }
PY="$BUILD_VENV/bin/python"

echo ">>> Installing project + Nuitka toolchain"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install . nuitka ordered-set zstandard

echo ">>> Compiling with Nuitka (standalone onefile, $OS_SUFFIX)"
# First-party packages are listed explicitly: the CLI imports heavy deps lazily inside
# function bodies, so we make sure every mode's package is followed. --include-package-data
# bundles shared/media/workflows/*.json + shared/engine/*.txt; certifi ships the CA bundle
# httpx needs for TLS to the relay/control plane.
"$PY" -m nuitka \
  --standalone \
  --onefile \
  --deployment \
  --output-dir="$REPO_ROOT/dist" \
  --output-filename=grid \
  --remove-output \
  --assume-yes-for-downloads \
  --company-name="LocalAGI" \
  --product-name="Grid CLI" \
  --product-version="0.1.0" \
  --include-package=cli \
  --include-package=shared \
  --include-package=lan \
  --include-package=internet \
  --include-package=uvicorn \
  --include-package-data=shared \
  --include-package-data=certifi \
  --onefile-tempdir-spec="{CACHE_DIR}/localagi-grid/{VERSION}-${OS_SUFFIX}" \
  packaging/grid_entry.py

echo
echo "Built: $REPO_ROOT/dist/grid"
