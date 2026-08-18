#!/bin/sh
# The flow a desktop application drives (ADR 0034 D-m, issue 46).
#
# Create a conversation, send two more messages into it, watch the whole conversation across the
# turn boundary, read the result, and branch on success or failure — using ONLY `--json` and exit
# codes. No screen-scraping, no git, and nothing that reads a sentence.
#
# It is a committed test fixture AND the worked example `docs/cli.md` points at, which is the reason
# it is a script rather than a Python driver: an application author copies this.
#
# ## Two things about it are deliberate and easy to "fix" back
#
#   * `rc=$?`, never `status=$?`. In zsh — the default macOS shell — `status` is a READ-ONLY special
#     parameter aliased to `$?`, so the assignment fails, the comparison then reads the assignment's
#     own 1, and the loop reports a FAILURE on the first poll of a perfectly healthy running task.
#     bash and sh accept `status` happily, which is what makes the trap invisible to whoever writes
#     it.
#   * JSON is read with `python3`, not `jq`. `grid` is a Python program, so python3 is present
#     wherever it runs; `jq` is not installed on a stock provider host or a stock CI image, and a
#     fixture that silently skipped there would be a test nobody notices has stopped running.
#
# Usage: app_flow.sh <project-id>
# Exit:  0 every turn completed · 1 something ended badly · 2 the script itself could not proceed

set -u

PROJECT="${1:?usage: app_flow.sh <project-id>}"
GRID="${GRID_BIN:-grid}"

# Read one field out of a JSON document on stdin. Absent or unreadable is an empty string, and every
# caller checks for that — which is the whole point of `--json`: a missing key is a value, not a
# sentence to parse.
field() {
    python3 -c '
import json, sys
try:
    document = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
value = document.get(sys.argv[1]) if isinstance(document, dict) else None
if value is not None:
    print(value)
' "$1"
}

say() { printf '%s\n' "$*" >&2; }

# --- 1. Open a conversation with its first message -------------------------------------------
created=$("$GRID" task create --project "$PROJECT" --prompt 'WRITE one.txt FIRST; SAY opened' --json)
rc=$?
if [ "$rc" -ne 0 ]; then
    say "could not open a conversation (exit $rc)"
    exit 2
fi

conversation=$(printf '%s' "$created" | field conversation_id)
first_turn=$(printf '%s' "$created" | field id)
if [ -z "$conversation" ] || [ -z "$first_turn" ]; then
    say "the create reply named no conversation to send into"
    exit 2
fi
say "conversation $conversation opened by turn $first_turn"

# --- 2. Wait for the first turn, so the second is a real follow-up ----------------------------
# `grid task get` exits 2 while a turn is unfinished, 0 when it completed, 1 when it ended badly.
# `2` is the ONLY code a poller may read as "ask again"; 1 covers both a failed turn and a relay
# that could not be reached, so treating it as "keep waiting" is how a script waits forever.
attempts=0
while :; do
    "$GRID" task get "$first_turn" --json >/dev/null
    rc=$?
    [ "$rc" -eq 2 ] || break
    attempts=$((attempts + 1))
    if [ "$attempts" -gt 120 ]; then
        say "the first turn never finished"
        exit 2
    fi
    sleep 1
done
if [ "$rc" -ne 0 ]; then
    say "the first turn ended badly (exit $rc)"
    exit 1
fi

# --- 3. Send two more messages, typed ahead ----------------------------------------------------
# They queue behind each other inside the conversation, which is the point: an application does not
# have to wait for one before offering the next.
for prompt in 'READ one.txt; WRITE two.txt SECOND; SAY second' 'READ two.txt; SAY third'; do
    sent=$("$GRID" task send "$conversation" --prompt "$prompt" --json)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        say "could not send into the conversation (exit $rc)"
        exit 2
    fi
    last_turn=$(printf '%s' "$sent" | field id)
    [ -n "$last_turn" ] || { say "a send named no turn"; exit 2; }
    say "sent turn $last_turn"
done

# --- 4. Follow the WHOLE conversation, across the turn boundary --------------------------------
# One stream, one cursor, every turn in order — including steps the grid added itself. It ends by
# itself when the conversation goes quiet, which is what makes this a `wait` an application can do.
"$GRID" task follow --conversation "$conversation" --json > "${FOLLOW_LOG:-/dev/null}"
rc=$?
if [ "$rc" -ne 0 ]; then
    say "following the conversation ended badly (exit $rc)"
    exit 1
fi

# --- 5. Read the outcome and branch on it ------------------------------------------------------
"$GRID" task get "$last_turn" --json > "${RESULT_JSON:-/dev/null}"
rc=$?
case "$rc" in
    0) say "every turn completed" ;;
    1) say "the conversation ended badly"; exit 1 ;;
    *) say "the last turn is still unfinished"; exit 2 ;;
esac

# --- 6. And read the project back, with no git on this machine ---------------------------------
"$GRID" project files "$PROJECT" --json > "${FILES_JSON:-/dev/null}"
rc=$?
if [ "$rc" -ne 0 ]; then
    say "could not read the project back (exit $rc)"
    exit 1
fi

exit 0
