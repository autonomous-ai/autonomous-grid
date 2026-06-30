#!/usr/bin/env bash
#
# Remote-mode E2E test for the grid CLI (v0.1.1) — TWO HOSTS.
#
#   MacBook    = admin + consumer  (creates/starts the grid, then consumes the model)
#   Mac Studio = provider          (serves a local engine to the grid through the relay)
#
# Remote mode talks to autonomous's HOSTED RELAY, so the two hosts do NOT need to be
# on the same local network — they meet over the internet. Sign in with the SAME account on both.
# Because one account both serves and consumes, the consumer passes --allow-self-provider.
#
# Copy this file to BOTH hosts (it only needs `grid` + `curl`). Run the steps IN ORDER,
# on the host shown in [brackets]:
#
#   1. [both]        ./test_remote_mode_e2e.sh login     # same account on both hosts
#   2. [MacBook]     ./test_remote_mode_e2e.sh up        # create + start the grid
#   3. [Mac Studio]  ./test_remote_mode_e2e.sh serve     # provider joins its engine
#   4. [MacBook]     ./test_remote_mode_e2e.sh chat      # <-- core round-trip test
#   5. [MacBook]     ./test_remote_mode_e2e.sh env       # OpenAI-compatible env for apps
#   6. [MacBook]     ./test_remote_mode_e2e.sh members   # membership admin (remote-only)
#   7. [Mac Studio]  ./test_remote_mode_e2e.sh leave     # stop serving (optional)
#   8. [MacBook]     ./test_remote_mode_e2e.sh down      # take the grid offline
#   9. [both]        ./test_remote_mode_e2e.sh logout    # sign out
#
set -uo pipefail

# ----- CONFIG (override via env, e.g. `MODEL=qwen3-coder ./... serve`) ----------
GRID_NAME="${GRID_NAME:-itest-grid}"
MODEL="${MODEL:-minimax-m2}"
ENGINE_URL="${ENGINE_URL:-http://127.0.0.1:58081/v1}"   # Mac Studio llama.cpp — NON-default port => --at required
PROVIDER_NAME="${PROVIDER_NAME:-macstudio}"
MEMBER_EMAIL="${MEMBER_EMAIL:-teammate@example.com}"
# Relay target: the installed 0.1.1 build defaults to PROD (autonomous.ai) for BOTH the
# control plane and the website, so login works out of the box. To test another env,
# export BOTH before `login` (they MUST match the same env, or login hangs at approve):
#   export GRID_CONTROL_PLANE_URL="https://api-grid.autonomous.ai"
#   export GRID_WEBSITE_URL="https://autonomous.ai"
# -------------------------------------------------------------------------------

bold(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok(){   printf '  \033[32m✓ %s\033[0m\n' "$*"; }
warn(){ printf '  \033[33m! %s\033[0m\n' "$*"; }
run(){  printf '  \033[2m$ %s\033[0m\n' "$*"; "$@"; }

preflight(){
  command -v grid >/dev/null || { echo "grid is not on PATH — install the wheel first"; exit 1; }
  local v; v="$(grid --version 2>/dev/null || true)"
  printf '  using: %s  (%s)\n' "$v" "$(command -v grid)"
  case "$v" in *0.1.1*) ok "version 0.1.1" ;; *) warn "expected 0.1.1 — is this the NEW build?" ;; esac
  # Prove it's the local/remote build: the old 'internet' spelling must be rejected.
  if grid mode internet >/dev/null 2>&1; then warn "'grid mode internet' was ACCEPTED — this is the OLD internet build!"; else ok "'internet' mode is gone (local/remote build confirmed)"; fi
}

step_login(){
  bold "[both hosts] sign in to remote mode (use the SAME account on both)"
  preflight
  run grid mode remote
  echo "  -> a browser opens; approve the device code. (headless box? add --no-browser to print the code)"
  if grid login; then ok "logged in"; else
    warn "login failed — if it HUNG at approve, control-plane/website env are mismatched; export matching"
    warn "GRID_CONTROL_PLANE_URL + GRID_WEBSITE_URL (same env) and retry."; exit 1
  fi
  bold "grids this account can reach:"; grid ls || true
}

step_up(){
  bold "[MacBook] create + start the remote grid"
  preflight
  echo "  GOTCHA: 'grid up' only CREATES on the 1st call; run it AGAIN to START. join/chat need status=running."
  run grid up "$GRID_NAME" --type permissioned-public || true    # 1st call: create
  sleep 2
  run grid up "$GRID_NAME" || true                                # 2nd call: start
  run grid use "$GRID_NAME"
  bold "status (want it 'running'):"; grid info "$GRID_NAME" || true
  ok "grid '$GRID_NAME' should be running — now run 'serve' on the Mac Studio"
}

step_serve(){
  bold "[Mac Studio] serve the local engine to '$GRID_NAME' (provider)"
  preflight
  printf '  engine check (%s): ' "$ENGINE_URL"
  if curl -fsS "$ENGINE_URL/models" >/dev/null 2>&1; then ok "reachable"; else
    warn "NOT reachable — start your engine first (llama.cpp/vLLM/etc) and check the port"; fi
  echo "  NON-default port => must pass --at + -m (auto-detect only finds engines on default ports)."
  run grid join "$GRID_NAME" --at "$ENGINE_URL" -m "$MODEL" --name "$PROVIDER_NAME"
  echo "  (spawns the detached __remote-engine that polls the relay outbound — no inbound port needed)"
  warn "'grid models' / 'grid engines' are STUBBED in remote mode — confirm via a successful chat, not those."
  ok "provider is serving — now run 'chat' on the MacBook"
}

step_chat(){
  bold "[MacBook] consume the model through the relay — THE core round-trip"
  preflight
  grid use "$GRID_NAME" >/dev/null 2>&1 || true
  echo "  same account serves+consumes => --allow-self-provider is required (else the relay refuses to self-route)."
  bold "plain chat:"
  run grid chat -m "$MODEL" --allow-self-provider "Reply with exactly one word: PONG" || warn "chat failed"
  bold "full JSON (reasoning models put the text in reasoning_content if 'content' looks empty):"
  grid chat -m "$MODEL" --allow-self-provider --json "Say hello in five words." || warn "chat --json failed"
  ok "saw a model reply above? then remote serve→relay→consume works end-to-end."
}

step_env(){
  bold "[MacBook] OpenAI-compatible env for your apps"
  preflight
  grid use "$GRID_NAME" >/dev/null 2>&1 || true
  run grid info --env
  cat <<EOF

  Optional raw round-trip with those values (self-route needs the header):
    eval "\$(grid info --env)"
    curl -sS "\$OPENAI_BASE_URL/chat/completions" \\
      -H "Authorization: Bearer \$OPENAI_API_KEY" -H 'Content-Type: application/json' \\
      -H 'X-Allow-Self-Provider: true' \\
      -d '{"model":"$MODEL","messages":[{"role":"user","content":"ping"}]}'
EOF
}

step_members(){
  bold "[MacBook] membership admin (remote-only): add / list / remove"
  preflight
  grid use "$GRID_NAME" >/dev/null 2>&1 || true
  echo "  signature: grid members add|remove [grid] <email> [--role consumer|provider|both]"
  run grid members add "$GRID_NAME" "$MEMBER_EMAIL" || warn "add failed (you must be the grid's creator/admin)"
  bold "members (expect $MEMBER_EMAIL):"; grid members list "$GRID_NAME" || true
  run grid members remove "$GRID_NAME" "$MEMBER_EMAIL" || warn "remove failed"
  bold "members after remove:"; grid members list "$GRID_NAME" || true
}

step_leave(){
  bold "[Mac Studio] stop serving the engine"
  preflight
  run grid leave "$GRID_NAME" || warn "leave failed"
  ok "provider stopped"
}

step_down(){
  bold "[MacBook] take the grid offline (config persists)"
  preflight
  run grid down "$GRID_NAME" || warn "down failed"
}

step_logout(){
  bold "[both hosts] sign out"
  preflight
  run grid logout || warn "logout failed"
}

case "${1:-}" in
  login)   step_login ;;
  up)      step_up ;;
  serve)   step_serve ;;
  chat)    step_chat ;;
  env)     step_env ;;
  members) step_members ;;
  leave)   step_leave ;;
  down)    step_down ;;
  logout)  step_logout ;;
  *) sed -n '2,34p' "$0"; echo; echo "usage: $0 {login|up|serve|chat|env|members|leave|down|logout}"; exit 2 ;;
esac
