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

⚠️ **One prompt is not a script and must not be**: the RELAY's own merge prompt (ADR 0034 D-g, issue
42), which is the only text in this system an agent is handed by anything other than a person. It is
English, so the directive parser would meet `Resolve` and exit 2 — every merge turn failing for a
reason that has nothing to do with what is under test. `_resolve_the_merge` recognises it and does
the real thing with real git instead. What that buys is that the relay's ancestry check and the
provider's `ls-files --unmerged` guard are both judging a genuine two-parent commit that this file
did not fake.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
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
_VERSION = "2.1.251 (Claude Code)"


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


# How the relay asks for a merge (ADR 0034 D-g, issue 42). Matched on the INSTRUCTION rather than on
# a marker of its own, because there is no marker: the prompt is what reaches an agent, and a fake
# that keyed on something the relay would have to add would be testing a channel the real binary does
# not have. `refs/integrate/` is the prefix both repositories duplicate.
_MERGE_INSTRUCTION = re.compile(r"git merge (refs/integrate/[A-Za-z0-9._-]+)")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def _resolve_the_merge(ref: str) -> list[str]:
    """Do what the relay's prompt asks, with real git. Returns what to SAY about it.

    ⚠️ **The ref must already be HERE, and refusing otherwise is the honest check this file can
    make.** ADR 0033 D-e has the provider fetch `merge_ref` onto the identical local name before the
    spawn, precisely because the agent is handed no grid credential and must never get one. A fake
    that fetched it itself, or that shrugged when it was missing, would keep passing after the
    provider stopped fetching — and the real binary would then fail every merge turn with a git error
    nobody could trace back here.

    The resolution keeps BOTH sides, and `git add`s every conflicted path. Taking one side would
    satisfy the relay's ancestry check while destroying somebody's work, which is the failure ADR
    0033 issue 15 measured; staging nothing would leave the index unmerged, which the provider's own
    `ls-files --unmerged` guard fails the turn for. Both are real outcomes this fake must not
    accidentally produce.
    """
    present = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if present.returncode != 0:
        sys.stderr.write(
            f"fake claude: the grid asked me to merge {ref}, which is not in this repository. The "
            f"provider is supposed to fetch it onto that exact name before spawning me, and I have "
            f"no credential to fetch it myself (ADR 0033 D-e).\n")
        raise SystemExit(64)

    # `check=False`, because a CONFLICTING merge is the expected outcome and exits non-zero.
    merged = _git("merge", "--no-commit", "--no-ff", ref, check=False)
    # ⚠️ **But "it exited non-zero" is not "it conflicted", and not checking cost this file the one
    # honesty it has.** A merge refused for any other reason — a dirty tree left by an earlier step,
    # unrelated histories — leaves no `MERGE_HEAD`, so the unmerged list reads back EMPTY, the loop
    # below does nothing, and the commit at the end succeeds on whatever happened to be staged. This
    # process would then report a cheerful success having never merged `ref` at all, and the E2E
    # would be green on a provider that fetched nothing. `MERGE_HEAD` is the only artefact that says
    # a merge really started; git writes it for a conflicting merge and for a clean `--no-commit`
    # one alike.
    if _git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode != 0:
        sys.stderr.write(
            f"fake claude: `git merge {ref}` did not start a merge at all (exit "
            f"{merged.returncode}), so there is nothing here to resolve and reporting success would "
            f"be a lie: {merged.stderr.strip()!r}\n")
        raise SystemExit(64)
    unmerged = [path for path in _git(
        "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if path]
    for path in unmerged:
        ours = _git("show", f":2:{path}", check=False)
        theirs = _git("show", f":3:{path}", check=False)
        sides = [side.stdout for side in (ours, theirs) if side.returncode == 0]
        if not sides:
            # Deleted on both sides. `git rm` is how the INDEX is told that was the decision — the
            # relay's prompt says so, and an unstaged deletion reads as an unresolved path.
            _git("rm", "-q", "--", path)
            continue
        pathlib.Path(path).write_text("".join(sides), encoding="utf-8")
        _git("add", "--", path)
    _git("commit", "--quiet", "--no-edit", "-m", f"merge {ref}")
    return [f"combined {len(unmerged)} file(s)" if unmerged else "nothing to combine"]


def _script(prompt: str) -> str:
    """The part of `prompt` this fake is meant to execute, with any DELTA BLOCK dropped.

    Since ADR 0034 D-f (issue 43) the relay composes a short paragraph onto the FRONT of a turn's
    prompt when the project moved under that conversation — file names and a stat, in English. It is
    never stored, so what a person reads back is still what they typed; but it is what reaches an
    agent, and this fake treats the whole prompt as a script. Meeting it, the parser splits on the
    first `;`, finds a verb like `The`, and exits 2 — a broken AGENT, in a test about something else.

    CLAUDE.md predicted this exactly and said the first E2E to send a follow-up after a colleague's
    change lands would need it. That test is now `test_24`, and it flaked 2 runs in 3 before this:
    whether a block is composed depends on whether the sibling conversation's work had reached the
    trunk in time, which is a race the test does not control and should not have to.

    ⚠️ **Recognised STRUCTURALLY, duplicating no literal of the relay's wording** — the rule
    CLAUDE.md gives, and the reason it gives it: a copy of `turn_delta`'s prose here would be a
    cross-repo lockstep value that nothing checks, so a reworded block would go straight back to
    failing as `unknown directive`, and both suites would stay green.

    The structure it keys on is the one thing a script always has and prose never does: every
    directive this fake knows is an ALL-CAPS word (`WRITE`, `READ`, `SLEEP`, `SAY`, `FAIL`). So if
    the prompt does not START with one and it has a blank line, the script is what follows the last
    blank line. `.isupper()` rather than a list of the verbs, because a list here would be a second
    copy of the dispatcher below and could drift from it; what matters is not WHICH verb it is but
    that the opening token is shaped like one at all — `"The".isupper()` is False, and so is
    `"3".isupper()` for a block that opens with a count.
    """
    if prompt.strip().partition(" ")[0].isupper():
        return prompt  # starts with something shaped like a directive: an ordinary script.
    _, separator, tail = prompt.rpartition("\n\n")
    return tail if separator and tail.strip() else prompt


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

    if os.environ.get("GRID_E2E_GOAL_SCENARIO") == "subgoal":
        node = os.environ.get("GRID_E2E_GOAL_NODE")
        if node != "B" or not prompt.startswith("/goal ") or resume is not None:
            sys.stderr.write("fake Claude received an invalid child Goal assignment\n")
            return 2
        pathlib.Path("README.md").write_text(
            "# Child instructions\n\nOpen the finished parent artifact and follow the guide.\n")
        _emit({"type": "assistant", "message": {"usage": {
            "input_tokens": 15, "output_tokens": 10}, "content": [
            {"type": "text", "text": "Claude completed the child instructions"}]}})
        _emit({"type": "attachment", "attachment": {"type": "goal_status", "met": True,
               "reason": "README.md exists", "iterations": 1, "tokens": 25}})
        return 0

    # The mixed-harness Goal E2E. This is intentionally inside the ordinary fake Claude binary:
    # the provider must select Claude through the real capability claim, build `/goal` on the first
    # slice, and `--resume` the session on the next. No test helper calls this behavior directly.
    scenario = os.environ.get("GRID_E2E_GOAL_SCENARIO")
    if scenario in ("mixed", "mixed_eval_repair"):
        node = os.environ.get("GRID_E2E_GOAL_NODE")
        if node not in ("B", "D"):
            sys.stderr.write(f"fake claude goal unexpectedly reached node {node!r}\n")
            return 2
        if node == "D":
            if (scenario != "mixed_eval_repair" or resume != session
                    or prompt.startswith("/goal ")):
                sys.stderr.write("Claude D did not resume B's native Goal session\n")
                return 2
            if ("Grid's independent evaluation of the previous commit" not in prompt
                    or "required literal content is absent" not in prompt
                    or "addEventListener('click'" not in prompt):
                sys.stderr.write("Claude D did not receive relay-authored failed-eval guidance\n")
                return 2
            if not pathlib.Path("style.css").exists() or not pathlib.Path("README.md").exists():
                sys.stderr.write("Claude D did not receive Codex C's nominated result tree\n")
                return 2
            pathlib.Path("game.js").write_text(
                "let score=0;document.querySelector('#target').addEventListener('click',()=>{"
                "document.querySelector('#score').textContent=String(++score)});\n")
            _emit({"type": "assistant", "message": {"usage": {
                "input_tokens": 30, "output_tokens": 15}, "content": [
                {"type": "text", "text": "Claude D repaired the failed behavior eval"}]}})
            _emit({"type": "attachment", "attachment": {"type": "goal_status", "met": True,
                   "reason": "all independent behavior checks now pass", "iterations": 3,
                   "tokens": 45}})
            return 0
        if not pathlib.Path("game.js").exists():
            if not prompt.startswith("/goal ") or resume is not None:
                sys.stderr.write("fake claude did not receive native /goal on its first slice\n")
                return 2
            if ("Grid handoff for this distributed turn:" not in prompt
                    or "A completed feature 1" not in prompt):
                sys.stderr.write(
                    "fake claude started a native /goal but did not receive Codex A's "
                    "relay-authored turn handoff\n")
                return 2
            if not pathlib.Path("index.html").exists():
                sys.stderr.write("fake claude did not receive Codex's committed feature 1\n")
                return 2
            pathlib.Path("game.js").write_text(
                "let score=0;document.querySelector('#target').addEventListener('click',()=>{"
                "document.querySelector('#score').textContent=String(++score)});\n")
            _emit({"type": "assistant", "message": {"usage": {
                "input_tokens": 20, "output_tokens": 10}, "content": [
                {"type": "text", "text": "Claude completed feature 2"}]}})
            _emit({"type": "attachment", "attachment": {"type": "goal_status", "met": False,
                   "reason": "features 3 and 4 remain"}})
            # Grid interrupts here, after the evaluator checkpoint. If it does not, this timeout
            # makes the defect loud instead of letting one process consume the whole Goal.
            time.sleep(90)
            return 2
        if resume != session or prompt.startswith("/goal "):
            sys.stderr.write("fake claude reset /goal instead of resuming its native session\n")
            return 2
        pathlib.Path("partial-feature-34.tmp").write_text("Claude B died here\n")
        time.sleep(90)
        return 2

    merge = _MERGE_INSTRUCTION.search(prompt)
    if merge:
        # Not a script. See the module docstring: the relay wrote this one, in English, and the
        # directive parser below would meet `Resolve` and exit 2.
        said = _resolve_the_merge(merge.group(1))
        _emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
               "duration_ms": 1234, "session_id": session, "result": " ".join(said)})
        return 0

    said: list[str] = []
    turns = 0
    for directive in [d.strip() for d in _script(prompt).split(";") if d.strip()]:
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
