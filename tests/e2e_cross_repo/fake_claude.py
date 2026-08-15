#!/usr/bin/env python3
"""A stand-in for Claude Code that speaks the real `stream-json` wire.

It exists so the cross-repo E2E can drive the REAL provider and the REAL relay without spending a
subscription. Two things about it are deliberate:

* It parses the argv the provider actually builds (`-p`, `--output-format stream-json`, `--verbose`,
  `--permission-mode`, `--setting-sources user`, `--strict-mcp-config`, optional `--resume`) and
  refuses anything else — **and refuses argv that is MISSING any of them**. A fake that accepted
  whatever it was handed would keep passing after `agent_argv` changed shape. The last two are
  issue 22's, and requiring them here is what makes their removal break the cross-repo E2E: this
  process cannot honour a `.claude/settings.json` the way the real binary does, so the only thing it
  can honestly check is that the provider asked for the protection at all.
* It computes its transcript directory from ITS OWN `getcwd`, by the vendor's measured rule
  (`[^A-Za-z0-9]` -> `-`), and never from anything the provider tells it. That is the one property a
  self-consistent test suite cannot check: issue 06's silent bug was the provider planting its
  symlink at the UNRESOLVED path while the real binary wrote at the resolved one, and every unit
  test agreed with the bug because each compared our own computation against itself.

The prompt is the script. Directives are separated by `;`:

    WRITE <relative path> <content>   create a file in the workspace
    READ <relative path>              read one back and echo it as assistant text
    SLEEP <seconds>                   stay alive (a task that takes a while)
    FAIL <message>                    write to stderr and exit 1
    SAY <text>                        assistant text
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import uuid

_TRANSCRIPT_NAME = re.compile(r"[^A-Za-z0-9]")


def _emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


# What this fake claims to be when asked. `resolve_binary()` gates on a minimum version since issue
# 23, because `--settings` is the one flag that fails OPEN on a binary too old to understand its
# contents — so a stand-in that could not answer `--version` would fail every task here for a reason
# that has nothing to do with what these tests are about.
_VERSION = "2.1.223 (Claude Code)"


def _parse_argv(argv: list[str]) -> tuple[str, str | None]:
    verbose = False
    strict_mcp = False
    takes_a_value = {"-p": "prompt", "--output-format": "fmt", "--permission-mode": "mode",
                     "--setting-sources": "sources", "--resume": "resume",
                     "--settings": "settings"}
    seen: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in takes_a_value:
            seen[takes_a_value[arg]] = argv[i + 1]
            i += 2
        elif arg == "--verbose":
            verbose = True
            i += 1
        elif arg == "--strict-mcp-config":
            strict_mcp = True
            i += 1
        else:
            sys.stderr.write(f"fake claude: unexpected argument {arg!r}\n")
            raise SystemExit(64)
    prompt, fmt = seen.get("prompt"), seen.get("fmt")
    mode, resume = seen.get("mode"), seen.get("resume")
    # `sources == "user"` and not merely present: `--setting-sources project` would satisfy a
    # presence check while loading the very files issue 22 exists to keep out.
    if (prompt is None or fmt != "stream-json" or not verbose or not mode
            or seen.get("sources") != "user" or not strict_mcp):
        sys.stderr.write(f"fake claude: argv is not the shape the provider builds: {argv!r}\n")
        raise SystemExit(64)
    _require_the_confinement(seen.get("settings"), mode)
    return prompt, resume


def _require_the_confinement(settings: str | None, mode: str | None) -> None:
    """Issue 23's half, checked the only way this process honestly can.

    A fake cannot honour a sandbox — it has no kernel confinement, and pretending to would be a test
    agreeing with itself. What it CAN check is that the provider asked: `--settings` present, valid
    JSON, and a `sandbox` that is switched on. That makes dropping the policy break this free
    cross-repo run and not only the paid one in `tests/e2e_agent_sandbox.py`, which is the same
    bargain `--setting-sources` and `--strict-mcp-config` are held to above.

    `bypassPermissions` is refused for the same reason: measured on the real binary, that mode reads
    files outside the workspace with the whole policy in force, so an argv carrying both is a
    provider that looks confined and is not.
    """
    if settings is None:
        sys.stderr.write("fake claude: no --settings; the provider sent no confinement policy\n")
        raise SystemExit(64)
    try:
        policy = json.loads(settings)
    except ValueError as exc:
        sys.stderr.write(f"fake claude: --settings is not JSON ({exc}): {settings!r}\n")
        raise SystemExit(64) from exc
    if not policy.get("sandbox", {}).get("enabled"):
        sys.stderr.write(f"fake claude: --settings carries no enabled sandbox: {settings!r}\n")
        raise SystemExit(64)
    if mode == "bypassPermissions":
        # Checked, not merely described. This paragraph used to claim the refusal while `_parse_argv`
        # only tested that a mode was PRESENT — so the free cross-repo run would have stayed green
        # with the real guard weakened, which is the exact "claims to guard something, doesn't" shape
        # the rest of this file exists to catch.
        sys.stderr.write(
            "fake claude: --permission-mode bypassPermissions with a sandbox policy — measured on "
            "the real binary, that mode reads files outside the workspace with the whole policy in "
            "force, so this argv is a provider that looks confined and is not\n")
        raise SystemExit(64)


def _transcript_dir() -> pathlib.Path | None:
    """Where the REAL binary would write, derived from this process's own resolved cwd."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config:
        return None
    # `getcwd` has already followed every symlink on the way in — which is the whole point.
    return pathlib.Path(config) / "projects" / _TRANSCRIPT_NAME.sub("-", os.getcwd())


def _require_a_conversation_keyed_workspace() -> None:
    """Refuse a cwd that is not `…/projects/<project>/<member>/<conversation>/workspace`.

    The same kind of refusal this file already makes for the agent flags, and for the same reason:
    this fake cannot behave like Claude Code, so the honest thing it CAN check is that the provider
    asked for what the design requires. Since ADR 0034 D-c the workspace path IS the conversation's
    identity — the real binary derives a session's transcript directory from it — so a provider that
    dropped the conversation segment would run every conversation of a member in one directory, and
    each of them would resume the wrong session.

    Checked here rather than only in a test's assertions so that a dropped segment breaks the FREE
    cross-repo E2E and not only the paid one. Shape, never values: this process has no way to know
    which project or conversation it is serving, and inventing one would be a second place that has
    to agree with the relay about how a uuid is spelled.
    """
    parts = pathlib.Path(os.getcwd()).parts
    if len(parts) < 5 or parts[-1] != "workspace" or "projects" not in parts:
        sys.stderr.write(
            f"fake claude: cwd {os.getcwd()!r} is not a task workspace at all\n")
        raise SystemExit(64)
    projects = len(parts) - 1 - list(reversed(parts)).index("projects")
    # `projects + 1`, not `projects`: the marker is not one of the segments being counted. Getting
    # this wrong refuses every task with a message about the segment count being one too high —
    # which is what it did, and what the free E2E caught on its first run.
    below = parts[projects + 1:-1]
    if len(below) != 3:
        sys.stderr.write(
            f"fake claude: the workspace is keyed on {len(below)} segment(s) below `projects/` "
            f"({below!r}), and ADR 0034 D-c requires three — project, member key, conversation. "
            f"Two means every conversation of one member shares this directory, so each of them "
            f"resumes the wrong Claude Code session.\n")
        raise SystemExit(64)


def main() -> int:
    if sys.argv[1:2] == ["--version"]:
        # Answered before anything else, and without the argv check: this is how the provider's
        # version gate learns whether the binary understands `sandbox.*` settings at all.
        sys.stdout.write(f"{_VERSION}\n")
        return 0
    prompt, resume = _parse_argv(sys.argv[1:])
    _require_a_conversation_keyed_workspace()
    session = resume or str(uuid.uuid4())
    _emit({"type": "system", "subtype": "init", "session_id": session})

    transcript = _transcript_dir()
    if transcript is not None:
        transcript.mkdir(parents=True, exist_ok=True)
        # Appended, never rewritten: a resume continues the same file and keeps the same id.
        with (transcript / f"{session}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"sessionId": session, "prompt": prompt}) + "\n")

    said: list[str] = []
    turns = 0
    for directive in [d.strip() for d in prompt.split(";") if d.strip()]:
        verb, _, rest = directive.partition(" ")
        turns += 1
        if verb == "WRITE":
            where, _, content = rest.partition(" ")
            target = pathlib.Path(where)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            _emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"toolu_{turns}", "name": "Write",
                 "input": {"file_path": str(target)}}]}})
            _emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{turns}", "is_error": False}]}})
        elif verb == "READ":
            target = pathlib.Path(rest.strip())
            _emit({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"toolu_{turns}", "name": "Read",
                 "input": {"file_path": str(target)}}]}})
            body = target.read_text(encoding="utf-8") if target.exists() else "<missing>"
            said.append(body)
            _emit({"type": "assistant", "message": {"content": [
                {"type": "text", "text": body}]}})
        elif verb == "SLEEP":
            time.sleep(float(rest))
        elif verb == "SAY":
            said.append(rest)
            _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": rest}]}})
        elif verb == "FAIL":
            sys.stderr.write(f"{rest}\n")
            sys.stderr.flush()
            return 1
        else:
            sys.stderr.write(f"fake claude: unknown directive {verb!r}\n")
            return 2

    _emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
           "duration_ms": 1234, "session_id": session, "result": " ".join(said) or "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
