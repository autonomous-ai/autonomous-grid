#!/usr/bin/env bash
#
# End-to-end test for the mlx-omarchy engine (v0.4.2+) — ONE HOST, an M-series Mac.
#
# This script exists because nothing in `tests/` can prove the thing that matters: that the
# Apple GPU is REACHABLE — is_apple_silicon_linux()/vulkan_ready() read real files
# (/proc/device-tree/*, /usr/share/vulkan/icd.d/*), grid_engine_install spawns a real venv and
# downloads a real wheel, and mlx_lm.server is a real HTTP server. Every unit test stubs those.
# This script does not.
#
# TWO WAYS TO RUN IT:
#
#   A) REAL hardware — an M1/M2 Mac running Omarchy Mac / Omarchy MX Mac bare-metal, with
#      `vulkan-asahi` installed. This is the only run that proves the GPU path. Steps 1-9 below.
#
#   B) DEV mode — ANY aarch64 Linux box with software Vulkan (Try Omarchy's VM, a plain ARM64
#      Ubuntu). Proves the CLI's OWN code (venv, download, pip order, join/leave, the relay
#      round-trip) without proving the GPU path — the engine will run at CPU speed and say so.
#      Export GRID_MLX_OMARCHY_DEV=1 before `install` and `serve`. NEVER set this on a machine
#      real users will use — it exists for this script alone.
#
# Copy this file to the target host (it only needs `grid` + `curl`; on (A) also `vulkaninfo`).
# Run the steps IN ORDER:
#
#   1. ./test_omarchy_m_e2e.sh preflight   # device tree, Vulkan ICD, python3.14 — no writes
#   2. ./test_omarchy_m_e2e.sh login       # grid login — must show Omagrid (or your own grid)
#   3. ./test_omarchy_m_e2e.sh llama       # llama.cpp via Vulkan — the OTHER engine, sanity check
#   4. ./test_omarchy_m_e2e.sh install     # grid engine install mlx-omarchy
#   5. ./test_omarchy_m_e2e.sh serve       # grid join --serve <repo> --engine mlx-omarchy
#   6. ./test_omarchy_m_e2e.sh chat        # <-- core round-trip: a real generation through the relay
#   7. ./test_omarchy_m_e2e.sh stats       # grid stats --verbose: node named by chip, class gpu
#   8. ./test_omarchy_m_e2e.sh leave       # stop serving
#   9. ./test_omarchy_m_e2e.sh receipt     # everything above + uname/mesa/vulkaninfo -> one file
#
set -uo pipefail

GRID_NAME="${GRID_NAME:-itest-grid}"
MODEL="${MODEL:-mlx-community/Qwen2.5-0.5B-Instruct-4bit}"   # small: this is a wiring test, not a benchmark
ADVERTISE_AS="${ADVERTISE_AS:-mlxtest}"
PROVIDER_NAME="${PROVIDER_NAME:-$(hostname -s 2>/dev/null || echo omarchy-m-test)}"
RECEIPT_DIR="${RECEIPT_DIR:-./receipts}"

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

cmd_preflight() {
  say "Device identity (read-only — nothing here is written)"
  if [[ -r /proc/device-tree/compatible ]]; then
    compat="$(tr '\0' ' ' </proc/device-tree/compatible)"
    echo "compatible: $compat"
    [[ "$compat" == *"apple,"* ]] && ok "Apple Silicon device tree" || echo "  (not Apple Silicon — dev mode only, see header)"
  else
    echo "  no /proc/device-tree/compatible — not Linux, or not this hardware"
  fi
  if [[ -r /proc/device-tree/model ]]; then
    echo "model: $(tr '\0' ' ' </proc/device-tree/model)"
  fi
  if [[ -f /usr/share/vulkan/icd.d/asahi_icd.aarch64.json ]]; then
    ok "Honeykrisp ICD manifest present"
  else
    echo "  no Honeykrisp ICD — install it:  sudo pacman -S vulkan-asahi vulkan-icd-loader"
  fi
  command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>&1 | grep -i "deviceName\|driverName" || true
  command -v python3.14 >/dev/null && ok "python3.14 on PATH" || echo "  no python3.14 — uv will fetch one"
  command -v grid >/dev/null && ok "grid: $(grid --version)" || fail "grid not installed — curl -fsSL https://grid.autonomous.ai/install.sh | bash"
}

cmd_login() {
  say "Sign in and list grids"
  grid login || fail "grid login failed"
  grid grids || true
}

cmd_llama() {
  say "Sanity check: the OTHER built-in engine (llama.cpp via Vulkan)"
  grid engine install llama.cpp || fail "llama.cpp install failed"
  ok "llama.cpp installed — expect the log above to say 'via Vulkan', not 'no GPU detected'"
}

cmd_install() {
  say "grid engine install mlx-omarchy"
  grid engine install mlx-omarchy || fail "mlx-omarchy install failed"
  ok "mlx-omarchy installed"
}

cmd_serve() {
  say "grid join $GRID_NAME --serve $MODEL --engine mlx-omarchy"
  grid join "$GRID_NAME" --serve "$MODEL" --engine mlx-omarchy \
    --advertise-as "$ADVERTISE_AS" --name "$PROVIDER_NAME" \
    || fail "join failed"
  ok "joined — first start downloads the model (a few hundred MB to a few GB); this can take a while"
}

cmd_chat() {
  say "Round trip through the relay"
  grid models | grep -q "$ADVERTISE_AS" || fail "$ADVERTISE_AS is not in 'grid models' — join did not register"
  grid chat -m "$ADVERTISE_AS" "In one sentence, what makes this GPU unusual?" || fail "chat failed"
  ok "chat round trip succeeded"
}

cmd_stats() {
  say "grid stats --verbose — check the node name and class"
  grid stats --verbose
  echo "  Expect: named by CHIP (e.g. 'Apple M1 Pro'), not 'aarch64'; device_class 'gpu' (or"
  echo "  'server' if GRID_MLX_OMARCHY_DEV was used on hardware with no real Apple GPU)."
}

cmd_leave() {
  say "grid leave"
  grid leave --engine "$ADVERTISE_AS" || grid leave || true
  ok "left"
}

cmd_receipt() {
  say "Writing a receipt"
  mkdir -p "$RECEIPT_DIR"
  out="$RECEIPT_DIR/$(date -u +%Y-%m-%d)-$(hostname -s 2>/dev/null || echo host)-mlx-omarchy-e2e.md"
  {
    echo "# mlx-omarchy e2e run — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "- Host: \`$(hostname -s 2>/dev/null || echo unknown)\`"
    echo "- uname: \`$(uname -a)\`"
    [[ -r /proc/device-tree/model ]] && echo "- Model: \`$(tr '\0' ' ' </proc/device-tree/model)\`"
    [[ -r /proc/device-tree/compatible ]] && echo "- Compatible: \`$(tr '\0' ' ' </proc/device-tree/compatible)\`"
    command -v vulkaninfo >/dev/null && echo "- Vulkan: \`$(vulkaninfo --summary 2>&1 | grep -m1 driverName || true)\`"
    echo "- grid: \`$(grid --version 2>&1)\`"
    echo "- GRID_MLX_OMARCHY_DEV: \`${GRID_MLX_OMARCHY_DEV:-unset}\`"
    echo
    echo "## grid stats --verbose"
    echo '```'
    grid stats --verbose 2>&1
    echo '```'
  } > "$out"
  ok "wrote $out"
}

cmd="${1:-}"
case "$cmd" in
  preflight|login|llama|install|serve|chat|stats|leave|receipt) "cmd_$cmd" ;;
  ""|help|-h|--help)
    sed -n '3,32p' "$0"
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    exit 1
    ;;
esac
