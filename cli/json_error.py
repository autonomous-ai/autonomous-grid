"""A refusal an application can branch on (ADR 0034 D-m, issue 46).

The client is Flutter on desktop spawning this binary as a subprocess. Every command it drives has
`--json`, so every SUCCESS it sees is a document — and every FAILURE was a sentence on stderr, which
is the one thing a program must not parse: reword the sentence and the client breaks with nothing
red anywhere. `remote/relay.TaskRefusal` has carried the relay's machine-readable code since
ADR 0033 D-l, and exactly two branches in this CLI read it; nothing ever put it where a caller could
see it.

## What it does NOT change, deliberately

**The exception is re-raised, not swallowed.** `SystemExit` leaving `main()` is this plane's whole
error contract — `TaskRefusal`'s docstring counts twelve `except SystemExit` sites across `cli/` and
`remote/`, and eight tests in this repository put `pytest.raises(SystemExit)` around a `--json`
command. Returning an exit code here instead would be a second failure shape for every one of them
to learn. The envelope is an ADDITION to stderr and nothing else moves: same type, same message,
same status.

**stdout is untouched.** `--json`'s stdout is the documents the relay sent, and stays so — the rule
`remote_task._note_an_unreadable_state` already follows for its own disclosure. A consumer reading
`create --follow --json` line by line therefore never has to tell an event from an error.

**The human sentence still goes out.** The interpreter prints `str(exc)` on its way out as it always
has; the envelope is beside it, not instead of it, so anything watching the terminal is unaffected.
"""
from __future__ import annotations

import argparse
import json
import sys


def asked_for_json(argv: list[str] | None) -> bool:
    """Whether `--json` is in a RAW argv, for the one caller that has no parsed namespace.

    argparse refuses an argv it cannot parse by raising `SystemExit(2)` itself, so on that path
    there is no `Namespace` to ask — and that is the path a version-skewed application lands on,
    which makes it the one that most needs an answer a program can read.

    ⚠️ **A bare substring match, and the imprecision is deliberate and bounded.** `--json` appearing
    as somebody's PROMPT text (`grid task create --prompt "--json"`) reads as True here. This
    function is only ever consulted when the command has ALREADY failed to parse, so the whole cost
    of being wrong is one extra document on stderr beside a usage error — against the cost of being
    wrong the other way, which is the failure this module exists to remove.
    """
    return "--json" in (argv or ())


def refuse_as_json(args: argparse.Namespace | None, exc: SystemExit, *,
                   argv: list[str] | None = None) -> None:
    """Write `exc` to stderr as one JSON document, when `--json` was asked for. Otherwise silent.

    Called from `main()`'s `except SystemExit`, which then re-raises — see the module docstring.
    `args` is `None` when argparse itself refused; `argv` is then what says whether `--json` was
    asked for.

    ⚠️ **A clean exit is not a refusal.** `SystemExit(None)` and `SystemExit(0)` are how a command
    stops early having succeeded, and `sys.exit(0)` inside a handler must not be reported to a
    client as an error. `SystemExit.code` holds whatever was passed, so the two shapes are told
    apart by looking at it rather than by assuming every `SystemExit` is a failure — the same
    distinction `TaskRefusal` documents at the other end, where a code of `None` is what made a
    failed create exit 0 with nothing printed.
    """
    if not (getattr(args, "json", False) if args is not None else asked_for_json(argv)):
        return
    code = exc.code
    if code is None or code == 0:
        return
    # `code` and not `str(exc)`: the two agree today, but `code` is the value the interpreter itself
    # will act on, so reporting anything else would describe a different failure from the one about
    # to happen.
    #
    # ⚠️ **The envelope always carries a NON-EMPTY message**, and that is one invariant rather than
    # two special cases. An empty one tells an application an error was described when nothing was —
    # worse than a clumsy sentence, because there is nothing to put in front of a person. Two shapes
    # reach it: `SystemExit(<int>)`, which carries no sentence at all, and `SystemExit("")`, which a
    # `raise SystemExit(some_variable)` produces when the variable is empty. Neither is raised
    # anywhere in this CLI today — every refusal here carries words, and the one numeric path
    # (`grid task get`'s exit 2) is a `return` rather than a raise — so this is a floor under future
    # code, not a description of current behaviour. The alternatives are hiding a real failure, or
    # raising from inside the handler that exists to report one.
    message = "" if isinstance(code, int) else str(code)
    message = message or f"the command failed with exit status {exc.code if isinstance(code, int) else 1}"
    payload = {
        "error": {
            # `None` for a local refusal and for any relay sending a plain-string `detail` — the
            # ordinary answer, never an error. A client branches on the code when there is one and
            # shows `message` when there is not, which is exactly what this CLI itself does.
            "code": getattr(exc, "refusal_code", None),
            "message": message,
            # The relay's HTTP status when it answered, `None` when it never did. The difference
            # decides whether a client may treat the failure as a verdict or merely as "we could not
            # ask" — the same reasoning `RelayError.status` and `ControlPlaneError.status` record.
            "status": getattr(exc, "status", None),
        }
    }
    # One line, so a consumer reading stderr with `while read` gets one document per read — the
    # shape `create --follow --json` already uses on stdout.
    print(json.dumps(payload), file=sys.stderr)
