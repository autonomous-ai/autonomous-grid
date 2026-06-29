#!/usr/bin/env bash
# Grid CLI installer.
#
#   curl -fsSL https://grid.autonomous.ai/install.sh | bash
#
# Hybrid by OS, because a *distributable* macOS binary needs Apple notarization
# (the ad-hoc Nuitka build is SIGKILL'd on modern macOS), while the same code runs
# fine under Python:
#   • Linux  → download a self-contained `grid` binary (no Python needed).
#   • macOS  → install the universal wheel with uv (bootstraps uv if missing).
# Either way it's one command and you end up with `grid` (+ the `agrid` alias).
#
# Knobs (all optional):
#   GRID_VERSION=0.1.0        pin a version (default: latest release)
#   GRID_INSTALL_DIR=~/bin    Linux binary location (default: ~/.local/bin)
#   GRID_REPO_OWNER / _NAME   source repo (default: autonomous-ai / autonomous-grid)
#   GRID_BASE_URL=https://…   Linux: fetch the binary + SHA256SUMS from a mirror
#   GRID_WHEEL_URL / GRID_PACKAGE   macOS: install this wheel URL / PyPI name instead
set -euo pipefail

OWNER="${GRID_REPO_OWNER:-autonomous-ai}"
REPO="${GRID_REPO_NAME:-autonomous-grid}"
VERSION="${GRID_VERSION:-latest}"
INSTALL_DIR="${GRID_INSTALL_DIR:-$HOME/.local/bin}"

info() { printf '\033[1;36m>>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"

arch="$(uname -m)"
case "$arch" in
  arm64|aarch64) arch_tag=arm64 ;;
  x86_64|amd64)  arch_tag=x86_64 ;;
  *) die "unsupported architecture: $arch" ;;
esac

tmp=""; trap 'rm -rf "${tmp:-}" 2>/dev/null || true' EXIT
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# --- Linux: self-contained binary, verified against the release's SHA256SUMS --
install_linux_binary() {
  local asset="grid-linux-${arch_tag}" base want got
  if   [ -n "${GRID_BASE_URL:-}" ]; then base="${GRID_BASE_URL%/}"
  elif [ "$VERSION" = latest ];    then base="https://github.com/$OWNER/$REPO/releases/latest/download"
  else                                  base="https://github.com/$OWNER/$REPO/releases/download/v$VERSION"; fi

  tmp="$(mktemp -d)"
  info "Downloading $asset ($VERSION)…"
  curl -fSL --proto '=https' --tlsv1.2 -o "$tmp/grid" "$base/$asset" \
    || die "download failed — no linux/$arch_tag build in the $VERSION release? ($base/$asset)"

  # Hard-fail on mismatch; only skip when the release ships no SHA256SUMS at all.
  if curl -fsSL --proto '=https' -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" 2>/dev/null; then
    want="$(grep -E "[[:space:]]${asset}\$" "$tmp/SHA256SUMS" | awk '{print $1}' | head -1)"
    if [ -n "$want" ]; then
      got="$(sha256_of "$tmp/grid")"
      [ "$got" = "$want" ] || die "checksum mismatch for $asset: got $got, want $want"
      ok "checksum verified"
    fi
  fi

  mkdir -p "$INSTALL_DIR"
  chmod +x "$tmp/grid"
  mv -f "$tmp/grid" "$INSTALL_DIR/grid"
  ln -sf grid "$INSTALL_DIR/agrid"   # match the wheel's two console scripts
  ok "installed to $INSTALL_DIR/grid"
}

# --- macOS: universal wheel via uv (uv installs both grid + agrid) ------------
install_macos_wheel() {
  local src api
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    info "Installing uv (Python runtime manager)…"
    curl -LsSf --proto '=https' https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      || die "could not install uv"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv not on PATH after install — open a new shell and retry"

  if   [ -n "${GRID_WHEEL_URL:-}" ]; then src="$GRID_WHEEL_URL"
  elif [ -n "${GRID_PACKAGE:-}"   ]; then src="$GRID_PACKAGE"
  else
    if [ "$VERSION" = latest ]; then api="https://api.github.com/repos/$OWNER/$REPO/releases/latest"
    else                            api="https://api.github.com/repos/$OWNER/$REPO/releases/tags/v$VERSION"; fi
    info "Resolving grid wheel from $OWNER/$REPO ($VERSION)…"
    src="$(curl -fsSL "$api" | grep -oE 'https://[^"]+grid-[^"]+-py3-none-any\.whl' | head -1)" \
      || die "could not query the $VERSION release"
    [ -n "$src" ] || die "no grid wheel asset in the $VERSION release"
  fi
  info "Installing grid…"
  uv tool install --force "$src" >/dev/null || die "uv tool install failed for $src"
}

case "$(uname -s)" in
  Linux)  install_linux_binary ;;
  Darwin) install_macos_wheel ;;
  *) die "unsupported OS: $(uname -s) — macOS and Linux only (Windows: see the docs)" ;;
esac

# --- locate the install, hint PATH, smoke-test -------------------------------
grid_bin="$(command -v grid || true)"
if [ -z "$grid_bin" ]; then
  for d in "$INSTALL_DIR" "$HOME/.local/bin"; do [ -x "$d/grid" ] && { grid_bin="$d/grid"; break; }; done
fi
[ -n "$grid_bin" ] || die "installed, but 'grid' isn't on PATH — add ~/.local/bin to PATH and reopen your shell"

bindir="$(cd "$(dirname "$grid_bin")" && pwd)"
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) info "Add $bindir to PATH:  echo 'export PATH=\"$bindir:\$PATH\"' >> ~/.zshrc && exec \$SHELL" ;;
esac

ver="$("$grid_bin" --version 2>&1)" || die "installed but failed to run: $ver"
ok "$ver"
info "Next:  grid up    # create your grid"
