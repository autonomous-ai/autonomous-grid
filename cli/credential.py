"""`grid credential <get|store|erase>` — git's credential-helper protocol (ADR 0033 D-h).

Not a command anybody types. `grid project clone` writes it into the clone's own `.git/config`, and
git runs it on every operation against that relay. The whole protocol lives in
`remote/git_credential.py`; this module is the I/O around it, and deliberately nothing else.

**Mode-agnostic** (`cli.dispatch.AGNOSTIC`). git invokes the helper in whatever mode `grid mode`
happens to be, and it reads only `credentials.toml`, which is not a mode-specific file — so gating
it on remote mode would break `git pull` inside a clone for a member who had switched to local.

**Every path git can reach exits 0**, and the three that could not are guarded by hand: an
unreadable stdin, an unreadable credential store (`load_toml` raises `SystemExit`, the CLI's clean-
error idiom everywhere else and exactly wrong here), and a stdout git has already closed. A helper
that fails is one git reports as an error over whatever the member was actually doing, and every
refusal here already says what is wrong on stderr, which git shows them.

The one non-zero exit left is argparse's own 2, for `grid credential` with no operation at all —
measured. git never does that (`gitcredentials(7)`: the operation argument is always appended), so
it is only reachable by a person typing the command by hand, where a usage message is the right
answer. Stated rather than claimed away, because "always exits 0" is the kind of absolute that
stops being true without anyone noticing.
"""
from __future__ import annotations

import argparse
import sys

# git's credential request is a handful of short lines. The cap is not a tuning knob — it is a bound
# on a pipe this process does not control, so a wedged or hostile writer cannot make the helper grow
# without limit in the middle of somebody's `git pull`.
_MAX_REQUEST_BYTES = 64 * 1024


def _read_request() -> bytes:
    """Whatever git wrote to stdin, bounded. Never raises.

    Through `.buffer` when there is one, because the protocol is bytes and the decode belongs in one
    place (`git_credential.parse_request`, which has an answer for undecodable input). A stdin with
    no `.buffer` is a test double or an unusual embedding, not an error.

    `OSError` covers the pipe being severed — git killed, the parent gone — which is an ordinary end
    to this conversation and not something to raise about. An empty request is answered the same way
    a request naming no host is: with no credential.
    """
    try:
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            return (sys.stdin.read(_MAX_REQUEST_BYTES) or "").encode("utf-8", "surrogatepass")
        return stream.read(_MAX_REQUEST_BYTES) or b""
    except OSError:
        return b""


def _networks() -> tuple[list, str]:
    """The stored grids, and a message if they could not be read. Never raises.

    `credentials.load_toml` raises **`SystemExit`** on an unreadable or corrupt store — the CLI's
    clean-error idiom everywhere else, and exactly wrong here. Nothing between this handler and the
    process boundary catches it (`dispatch` is a bare `args.handler(args) or 0`), so the helper
    would exit 1 and git would report a broken helper over whatever the member was really doing.

    Caught narrowly, around the one call that raises it, so this does not become a blanket that
    hides a future exit somebody meant.
    """
    from remote import credentials

    try:
        return (credentials.load_credentials().get("networks") or []), ""
    except SystemExit as exc:
        # `SystemExit(str)` — the message is the code. Re-prefixed so it reads as this tool's
        # diagnosis rather than as more of git's output.
        return [], f"grid: {exc}"


def cmd_credential(args: argparse.Namespace) -> int:
    from remote import git_credential

    networks, unreadable = _networks()
    reply = git_credential.respond(
        args.operation,
        _read_request(),
        networks=networks,
        network_id=getattr(args, "grid", None),
    )
    if reply.stdout:
        try:
            sys.stdout.write(reply.stdout)
            sys.stdout.flush()
        except OSError:
            # git closed its side. There is nobody left to tell, and raising would only turn a
            # finished conversation into a broken-helper report.
            return 0
    # The store's own failure outranks whatever `respond` concluded from an empty list, which would
    # otherwise be the misleading "that grid is not in your local credentials".
    message = unreadable or reply.stderr
    if message:
        print(message, file=sys.stderr, flush=True)
    return 0
