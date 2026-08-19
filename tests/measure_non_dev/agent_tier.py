"""Measurements 3, 4 and 5 — the ones that need a real Claude Code and a real conversation.

Transcript growth across ~50 turns, whether a compaction SHORTENS the `.jsonl`, and whether a
compacted transcript still resumes after a round trip through a git ref and re-materialization at a
different absolute path. Only the last is a product question, and it is the one issue 35's first
draft missed entirely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .report import MEASURED, CannotRun, Result, unavailable

#: The conversation came back: the token planted in turn 1 is in the answer.
RESUMED_WITH_MEMORY = "resumed_with_memory"
#: `--resume` exited 0 and the conversation is GONE. The failure that reads healthy, and the one a
#: boolean cannot express — the turn completes, the push lands, and the person's context is lost.
RESUMED_WITHOUT_MEMORY = "resumed_without_memory"
#: `--resume` would not start at all. The opposite conclusion to amnesia: the round trip broke the
#: transcript, rather than preserving a file that no longer carries the conversation.
RESUME_REFUSED = "resume_refused"


M3_GROWTH = "3-transcript-growth-per-turn"

#: Where a compaction may announce itself. The transcript schema is the VENDOR's and no compaction
#: record has been seen at first hand, so this looks structurally — a key whose NAME mentions
#: compaction, or a `type`/`subtype` whose VALUE does — and reports what it matched.
#:
#: Measured on 2.1.232: an ordinary transcript's `type` is one of queue-operation / attachment /
#: user / last-prompt / assistant and no key mentions compaction, so a false positive here would
#: have to be invented by the vendor rather than merely happen.
_MARKER_WORD = "compact"
_MARKER_VALUE_KEYS = ("type", "subtype")


def _markers_in(record, found: list[str]) -> None:
    """Every compaction-ish marker in one record, walked rather than pattern-matched at the top.

    Recursive because a nested `message` or `toolUseResult` is exactly where a vendor would put a
    summary record, and a top-level-only scan would answer "no compaction" for a transcript that
    plainly had one.
    """
    if isinstance(record, list):
        for item in record:
            _markers_in(item, found)
        return
    if not isinstance(record, dict):
        return
    for key, value in record.items():
        if _MARKER_WORD in key.lower():
            found.append(key)
        elif key in _MARKER_VALUE_KEYS and isinstance(value, str) \
                and _MARKER_WORD in value.lower():
            # The VALUE of a type field only. Never free text: a person or a model writing
            # "compact the CSS" would otherwise manufacture a compaction the run never had.
            found.append(f"{key}={value}")
        _markers_in(value, found)


def scan_compaction_markers(path) -> tuple[int, list[str]]:
    """How many records look like a compaction, and WHICH markers said so.

    The markers travel into the report beside the count, because this detector is the one part of
    the agent tier written against a schema nobody has seen a compaction in. A reader has to be
    able to check it was looking at the right thing — and if it was not, `decide_compaction`'s
    "shrank but nothing was recorded" branch is what catches the miss, rather than a false answer.
    """
    import json

    records, markers = 0, []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                # A partially-written last line is ordinary while the agent is running, and
                # `RecursionError` is a `RuntimeError` rather than a `ValueError` — naming only the
                # latter leaves the same hole `resumable_session` documents.
                continue
            here: list[str] = []
            _markers_in(record, here)
            if here:
                records += 1
                markers.extend(here)
    return records, sorted(set(markers))


M4_COMPACTION = "4-does-compaction-shorten-the-jsonl"


def _refuse_unread_samples(name: str, samples: list[dict]) -> Result | None:
    """Refuse, if any sample is a transcript that was never READ rather than one that was empty.

    ONE function because both `decide_growth` and `decide_compaction` consume the same `samples`
    list, and the second went without this guard while the first had it — which is exactly how a
    single missing sample became a fabricated "the compaction happened at turn N". A rule enforced
    in one of two places is a rule with a hole in it.

    `_sample` records an absent transcript as 0 bytes, and every such turn's SUBPROCESS exits 0 —
    the agent ran and wrote its transcript somewhere; only this harness looked in the wrong place.
    So the failed-turn counter structurally cannot catch it, and this is the only thing that does.
    Issue 06 shipped precisely this bug: the provider planted its symlink at the unresolved
    workspace path while the binary wrote at the resolved one.
    """
    absent = [sample["turn"] for sample in samples if sample.get("missing")]
    if not absent:
        return None
    return unavailable(
        name,
        f"the transcript was not found for {len(absent)} of {len(samples)} turns (first: turn "
        f"{absent[0]}), so those samples read 0 because nothing was READ, not because nothing was "
        f"written — check the transcript path derivation before reading anything into these "
        f"numbers")


def decide_growth(*, samples: list[dict]) -> Result:
    """What each turn ADDS to the transcript, and therefore what a turn pays before it starts.

    Issue 39 has to choose between fetching and pushing the side ref every turn and doing it only
    on change. That choice is affordable or not depending on the SHAPE of this curve, not its
    total: a constant per-turn cost is a fixed toll, and a growing one is a conversation that gets
    more expensive the longer somebody uses it — which is the opposite of what a person expects.

    So every turn's delta is kept, and the summary is derived from them rather than the other way
    round. A mean alone cannot distinguish the two shapes at all.
    """
    unread = _refuse_unread_samples(M3_GROWTH, samples)
    if unread is not None:
        return unread
    if len(samples) < 2:
        return unavailable(
            M3_GROWTH,
            f"growth needs at least two turns and {len(samples)} was recorded: a single sample's "
            f"delta is the whole file, which reads as a per-turn cost")
    turns, previous = [], 0
    for sample in samples:
        turns.append({"turn": sample["turn"], "bytes": sample["bytes"],
                      "lines": sample.get("lines"), "added_bytes": sample["bytes"] - previous})
        previous = sample["bytes"]
    added = [row["added_bytes"] for row in turns]
    first_half, second_half = added[:len(added) // 2], added[len(added) // 2:]
    return Result(
        name=M3_GROWTH, status=MEASURED,
        data={"turns": turns, "turn_count": len(turns),
              "total_bytes": samples[-1]["bytes"],
              "largest_turn_bytes": max(added), "smallest_turn_bytes": min(added),
              "mean_turn_bytes": round(sum(added) / len(added), 1),
              # The shape, stated as the two halves' means rather than as a verdict: a ratio near 1
              # is a fixed toll per turn, and one well above it is a conversation whose cost grows
              # with its own length. A reader can disagree with the reading; they cannot disagree
              # with the two numbers.
              "mean_first_half_bytes": round(sum(first_half) / len(first_half), 1),
              "mean_second_half_bytes": round(sum(second_half) / len(second_half), 1)})


def decide_compaction(*, samples: list[dict], compaction_records: int,
                      failed_turns: int = 0) -> Result:
    """Does a compaction SHORTEN the `.jsonl`? Answered only when a compaction actually happened.

    Two signals, deliberately independent: whether the file got smaller, and whether Claude Code
    recorded a compaction at all. Issue 35 names the conflation this prevents — "the ref advanced"
    and "the blob shrank" are different facts — and the four combinations are four different
    conclusions:

      * compacted and shrank        → it shortens. D-j's fast-forward-only rule has something real
                                      to reason about.
      * compacted and did not shrink→ it does NOT shorten. The most valuable outcome, and the one a
                                      size-only rule would report as "no compaction".
      * no compaction, no shrink    → NOTHING was observed. Not an answer. Reporting
                                      `shortens: false` here is a claim about an event that did not
                                      happen, which is precisely the conflation.
      * no compaction but it shrank → two readings and no way to choose. Named rather than guessed:
                                      a branch that quietly picked one is where the next version of
                                      this bug lands.
    """
    # BEFORE any size arithmetic: a 0 that was never read looks exactly like a shrink, and would
    # name the missing turn as the one a compaction happened at.
    unread = _refuse_unread_samples(M4_COMPACTION, samples)
    if unread is not None:
        return unread
    sizes = [int(sample["bytes"]) for sample in samples]
    shrank_at = next((samples[i]["turn"] for i in range(1, len(sizes))
                      if sizes[i] < sizes[i - 1]), None)
    if compaction_records <= 0 and failed_turns:
        # A fifth combination, and it was found by RUNNING this: twelve fill turns exited non-zero
        # (`--allowedTools` takes `<tools...>`, so the prompt after it was eaten as a tool name) and
        # the transcript stayed at exactly 99,628 bytes. Every other signal read "no compaction
        # occurred" — true, and completely misleading, because nothing had run. Checked BEFORE the
        # branches below so a failed phase can never be reported as an observation.
        return unavailable(
            M4_COMPACTION,
            f"{failed_turns} turn(s) FAILED, so the conversation never reached a size where a "
            f"compaction could fire — this says nothing about compaction at all. Fix the failing "
            f"turn before reading anything into these {len(samples)} samples")
    if compaction_records <= 0:
        if shrank_at is not None:
            return unavailable(
                M4_COMPACTION,
                f"the transcript SHRANK at turn {shrank_at} ({sizes}) but Claude Code recorded no "
                f"compaction, so the two possible explanations — an unrecorded compaction, or the "
                f"file being rewritten by something else — cannot be told apart from here")
        return unavailable(
            M4_COMPACTION,
            f"no compaction occurred in {len(samples)} turns, so nothing was observed about "
            f"whether one shortens the transcript. This is NOT the finding that compaction does "
            f"not shorten it — raise --turns or lower --autocompact and run again")
    return Result(
        name=M4_COMPACTION, status=MEASURED,
        data={"shortens": shrank_at is not None, "shrank_at_turn": shrank_at,
              "compaction_records": compaction_records,
              "bytes_by_turn": [{"turn": s["turn"], "bytes": s["bytes"]} for s in samples],
              "largest_bytes": max(sizes), "final_bytes": sizes[-1]})


def classify_resume(*, returncode: int, stdout: str, token: str) -> str:
    """Did the conversation survive? Three answers, because two of them look identical from outside.

    The probe is a token planted in the conversation's FIRST turn and asked for after the round
    trip. Nothing else works: a resumed session and a fresh one both answer fluently, both exit 0,
    and both report a session id — issue 06's bug is the standing proof that every signal except
    the content can be green while the conversation is gone.

    Matched as a whole word and case-insensitively. A bare substring test over a short token would
    let almost any fluent sentence count as a memory, which would turn this measurement into one
    that cannot fail.
    """
    if returncode != 0:
        return RESUME_REFUSED
    remembered = re.search(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", stdout,
                           re.IGNORECASE)
    return RESUMED_WITH_MEMORY if remembered else RESUMED_WITHOUT_MEMORY


def require_claude() -> str:
    """The Claude Code binary this tier will measure against, or a refusal naming it.

    `task_agent.resolve_binary()` rather than a search of our own — the provider's own resolution,
    so the harness measures the binary a task would actually spawn, and a machine where the provider
    could not run is a machine where this tier refuses for the same reason and in the same words.
    """
    from remote import task_agent

    try:
        return task_agent.resolve_binary()
    except RuntimeError as exc:
        raise CannotRun(str(exc)) from exc


M5_RESUME = "5-resume-a-compacted-transcript-after-a-git-round-trip"

#: Cheap on purpose: what is under measurement is the transcript and the resume, never the model's
#: reasoning. `e2e_live_agent.py` makes the same choice for the same reason.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
#: The documented minimum on 2.1.232 (`--autocompact <auto|tokens>`, "100k–1M tokens"), so a
#: compaction fires inside a bounded number of turns instead of waiting for a full context. This is
#: what makes measurement 4 reachable at all without a conversation nobody would pay for.
MIN_AUTOCOMPACT_TOKENS = 100_000
#: A member key's shape on the wire — 32 lower-case hex (`sha256(user_id)` truncated). The workspace
#: layout is built from it, so using a real-shaped one keeps this measuring the real path lengths.
_MEMBER_KEY = "0" * 32
_TURN_TIMEOUT_SECONDS = 600.0


def _claude(binary: str, workspace, *args: str, timeout: float = _TURN_TIMEOUT_SECONDS):
    """One `claude -p` turn, run the way `task_agent.agent_argv` runs one.

    `--setting-sources user` and `--strict-mcp-config` are carried deliberately: they are the two
    flags issue 22 calls a security boundary, and a measurement taken without them would be of a
    different argv than the provider spawns. `--setting-sources user` also turns off project-memory
    discovery, which is part of what the transcript's size reflects.
    """
    import subprocess

    argv = [binary, "-p", "--setting-sources", "user", "--strict-mcp-config", *args]
    return subprocess.run(argv, cwd=str(workspace), capture_output=True, text=True,
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def _new_workspace(root, name: str):
    """A workspace laid out and linked exactly as the provider lays one out.

    A real git repository, because measurement 5 puts the transcript through a real ref; and linked
    through `task_agent.link_transcript`, because the transcript directory's NAME is a contract with
    the vendor binary. Recomputing that name here instead would make the two agree by construction
    and prove nothing — issue 06's bug is the standing example of exactly that mistake.
    """
    from remote import task_agent

    from . import gitrun

    workspace = (root / name).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    gitrun.run("init", "--quiet", "--initial-branch=main", ".", cwd=workspace)
    (workspace / "README.md").write_text("measurement workspace\n", encoding="utf-8")
    gitrun.run("add", "-A", cwd=workspace)
    gitrun.run("commit", "--quiet", "-m", "workspace", cwd=workspace)
    link = task_agent.claude_config_dir() / "projects" / task_agent.transcript_dir_name(workspace)
    return workspace, task_agent.link_transcript(workspace, _MEMBER_KEY), link


#: Roughly how much text one filler file holds. Sized so a single read is worth ~12k tokens: big
#: enough that a handful of turns reach the 100k-token window, small enough to stay under the Read
#: tool's own truncation. DISTINCT content per file, since identical files would compress in
#: context and a model may legitimately decline to re-read one it has already seen.
_FILLER_BYTES = 48_000


def _plant_filler(workspace, *, count: int) -> list[str]:
    """`count` large, distinct, uninteresting files, and the paths to read them by.

    Uninteresting on purpose: the point is to consume context, and a file the model wants to reason
    about would add a variable this measurement cannot control. They live under the workspace
    because the agent is confined to it.
    """
    directory = workspace / "filler"
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = directory / f"filler-{index:03d}.txt"
        if not path.is_file():
            line = f"filler line for file {index:03d} — padding, no meaning, item "
            body = "\n".join(f"{line}{n}" for n in range(_FILLER_BYTES // len(line)))
            path.write_text(body + "\n", encoding="utf-8")
        paths.append(str(path))
    return paths


def _note_failure(failures: list[dict], done, turn: int) -> None:
    """Record a turn that did not run, so it can never be mistaken for one that ran and added little.

    This exists because the harness did exactly that: twelve consecutive fill turns exited non-zero
    and the run reported "no compaction occurred in N turns". A turn's return code is the only
    thing that separates "the conversation did not grow" from "nothing happened".
    """
    if done.returncode == 0:
        return
    failures.append({"turn": turn, "returncode": done.returncode,
                     "error": (done.stderr or done.stdout or "").strip()[:400]})


def _sample(path, turn: int) -> dict:
    if not path.is_file():
        return {"turn": turn, "bytes": 0, "lines": 0, "missing": True}
    text = path.read_bytes()
    return {"turn": turn, "bytes": len(text), "lines": text.count(b"\n")}


@dataclass(frozen=True)
class _Turn:
    """Everything one turn of the measured conversation needs, so the phases can be read.

    Six values threaded through two loops was what pushed `run_agent_tier` past this repo's
    fifty-line rule; bundling them is what lets each phase be a function with a name.
    """

    binary: str
    workspace: object
    transcript: object
    session_id: str
    model: str
    autocompact: int

    def resume(self, *args: str):
        """One `--resume` turn of this conversation, with the flags every turn shares."""
        return _claude(self.binary, self.workspace, "--resume", self.session_id,
                       "--model", self.model, "--autocompact", str(self.autocompact),
                       "--output-format", "json", *args)


def _phase_growth(turn: _Turn, *, turns: int) -> tuple[list[dict], list[dict]]:
    """Phase A — `turns` short turns, sampling the transcript after each. Measurement 3."""
    samples = [_sample(turn.transcript, 1)]
    failures: list[dict] = []
    for n in range(2, turns + 1):
        done = turn.resume(f"Reply with only the number {n}.")
        _note_failure(failures, done, n)
        samples.append(_sample(turn.transcript, n))
        print(f"  turn {n}/{turns}: {samples[-1]['bytes']} bytes"
              f"{'  FAILED' if done.returncode != 0 else ''}", flush=True)
    return samples, failures


def _phase_force_compaction(turn: _Turn, samples: list[dict], failures: list[dict], *,
                            turns: int, fill_turns: int) -> tuple[int, list[str]]:
    """Phase B — grow the context until Claude Code records a compaction. Measurement 4.

    Reading a large file, not asking for a long reply. MEASURED in a smoke run: a turn of short
    prompts adds ~13 KB of transcript, so filling a 100k-token context that way needs far more
    turns than anybody would pay for. A tool RESULT lands in the context whole, so one read of a
    ~48 KB file is worth roughly a dozen chatty turns.

    A different file each turn, because re-reading one is exactly the case an agent is entitled to
    satisfy from what it already has.
    """
    filler = _plant_filler(turn.workspace, count=fill_turns)
    records, markers = scan_compaction_markers(turn.transcript)
    for extra in range(1, fill_turns + 1):
        if records:
            break
        done = turn.resume(
            # `--allowedTools=Read`, NOT `--allowedTools Read`. Measured on 2.1.232: the flag takes
            # `<tools...>` space-separated, so the separated form eats the PROMPT as a second tool
            # name and the run dies with "No deferred tool marker found … Provide a prompt to
            # continue the conversation". The `=` form binds exactly one value.
            "--allowedTools=Read",
            f"Read the file {filler[extra - 1]} and reply with only the word DONE.")
        _note_failure(failures, done, turns + extra)
        samples.append(_sample(turn.transcript, turns + extra))
        records, markers = scan_compaction_markers(turn.transcript)
        print(f"  fill {extra}/{fill_turns}: {samples[-1]['bytes']} bytes, "
              f"{records} compaction record(s)"
              f"{'  FAILED' if done.returncode != 0 else ''}", flush=True)
    return records, markers


def run_agent_tier(scratch, *, binary: str, turns: int = 50, model: str = DEFAULT_MODEL,
                   autocompact: int = MIN_AUTOCOMPACT_TOKENS,
                   fill_turns: int = 25) -> list[Result]:
    """Measurements 3, 4 and 5 — ONE conversation, three phases, in that order because each needs
    what the previous produced.

    Phase A is `turns` short turns: the growth curve (measurement 3). Phase B keeps going with
    context-filling turns until Claude Code records a compaction or `fill_turns` is spent
    (measurement 4). Phase C takes whatever phase B produced through a real git ref into a workspace
    at a DIFFERENT absolute path and asks for the token planted in turn 1 (measurement 5).

    ⚠️ This runs against the operator's OWN `~/.claude`, and it must: issue 01's spike measured a
    fresh `CLAUDE_CONFIG_DIR` answering `Not logged in` even on macOS, and seeding an account file
    did not help. `e2e_live_agent.py` carries the same note. Everything planted there is removed in
    the `finally`.
    """
    import uuid


    planted: list = []
    try:
        workspace, transcript_home, link = _new_workspace(scratch, "conversation")
        planted.append(link)
        session_id = str(uuid.uuid4())
        token = f"XYZZY-{uuid.uuid4().hex[:8].upper()}"
        transcript = transcript_home / f"{session_id}.jsonl"

        opened = _claude(binary, workspace, "--session-id", session_id, "--model", model,
                         "--autocompact", str(autocompact), "--output-format", "json",
                         f"Reply with only the word OK. Remember this exactly: {token}")
        if opened.returncode != 0:
            return [unavailable(name, f"the conversation's first turn exited {opened.returncode}: "
                                      f"{(opened.stderr or opened.stdout).strip()[:400]}")
                    for name in (M3_GROWTH, M4_COMPACTION, M5_RESUME)]

        turn = _Turn(binary=binary, workspace=workspace, transcript=transcript,
                     session_id=session_id, model=model, autocompact=autocompact)
        samples, failures = _phase_growth(turn, turns=turns)
        growth = (decide_growth(samples=samples) if not failures else unavailable(
            M3_GROWTH,
            f"{len(failures)} of {turns} turns failed, so the curve has gaps where a turn added "
            f"nothing because it never ran: {failures[0]['error'][:200]}"))

        records, markers = _phase_force_compaction(turn, samples, failures, turns=turns,
                                                   fill_turns=fill_turns)
        compaction = decide_compaction(samples=samples, compaction_records=records,
                                       failed_turns=len(failures))
        if markers:
            print(f"  compaction markers seen: {markers}", flush=True)

        # ── Phase C · the round trip, at a DIFFERENT absolute path ────────────────────────────
        # Isolated, and that is worth the `except`: by this point growth and compaction have both
        # SUCCEEDED and cost fifty paid turns, and they sit in local variables. A raise out of the
        # round trip's git — a corrupt checkout, disk pressure — would unwind past the return and
        # lose all three, then `cli.main`'s `finally` would delete the scratch that explains why.
        # Phase C's failure is phase C's result, not the run's.
        try:
            resume = _round_trip_and_resume(scratch, workspace, transcript, session_id=session_id,
                                            token=token, binary=binary, model=model,
                                            compacted=bool(records), planted=planted,
                                            markers=markers, transcript_bytes=samples[-1]["bytes"])
        except (OSError, RuntimeError) as exc:
            resume = unavailable(
                M5_RESUME,
                f"the round trip could not be completed, so nothing was learned about resuming a "
                f"compacted transcript: {exc}")
        return [growth, compaction, resume]
    finally:
        # The operator's real config directory gets its symlinks back the way it had them. A run
        # that left one behind would point a future session at a deleted scratch directory.
        for link in planted:
            try:
                if link.is_symlink():
                    link.unlink()
            except OSError:
                print(f"  warning: could not remove {link}", flush=True)


def _round_trip_and_resume(scratch, workspace, transcript, *, session_id: str, token: str,
                           binary: str, model: str, compacted: bool, planted: list,
                           markers: list, transcript_bytes: int) -> Result:
    """Measurement 5 — the product question, not the git one.

    The transcript goes onto `refs/grid/agent/<id>` in a bare repo, is FETCHED into a second bare
    repo (a real round trip, not a copy), materialized into a workspace at a different absolute
    path, and resumed there. The different path is the whole point: Claude Code derives the
    transcript directory from its resolved cwd, so a provider that picks up somebody else's
    conversation is always resuming at a path the conversation was not written at.
    """
    from remote import task_agent

    from . import gitrun
    from .report import MEASURED

    if not compacted:
        return unavailable(
            M5_RESUME,
            "no compaction was observed, so the question asked here — whether a COMPACTED "
            "transcript still resumes after a round trip — was not put. A round trip of an "
            "uncompacted transcript answers a different and easier question, and reporting it "
            "under this name is how the two would get conflated")

    origin = scratch / "origin.git"
    gitrun.run("init", "--bare", "--quiet", str(origin))
    gitrun.run("add", "--force", "--", str(transcript.relative_to(workspace)), cwd=workspace)
    gitrun.run("commit", "--quiet", "-m", "transcript", cwd=workspace)
    head = gitrun.run("rev-parse", "HEAD", cwd=workspace).stdout.strip()
    gitrun.run("push", "--quiet", str(origin), f"HEAD:refs/grid/agent/{session_id}", cwd=workspace)

    mirror = scratch / "mirror.git"
    gitrun.run("init", "--bare", "--quiet", str(mirror))
    gitrun.run("-C", str(mirror), "fetch", "--quiet", str(origin),
               f"+refs/grid/agent/{session_id}:refs/grid/agent/{session_id}")

    elsewhere, transcript_home, link = _new_workspace(scratch / "elsewhere", "conversation")
    planted.append(link)
    gitrun.run("fetch", "--quiet", str(mirror),
               f"+refs/grid/agent/{session_id}:refs/grid/agent/{session_id}", cwd=elsewhere)
    gitrun.run("checkout", "--quiet", f"refs/grid/agent/{session_id}", "--",
               str(transcript.relative_to(workspace)), cwd=elsewhere)

    landed = transcript_home / f"{session_id}.jsonl"
    if not landed.is_file():
        return unavailable(M5_RESUME,
                           f"the transcript did not arrive at {landed} after the round trip")
    # What the provider's own gate would say about this file. Recorded because that gate checks
    # only that the first line parses as a JSON object — so a PASS here beside a lost conversation
    # is itself the finding.
    gate = task_agent.resumable_session(elsewhere, session_id, _MEMBER_KEY)

    answer = _claude(binary, elsewhere, "--resume", session_id, "--model", model,
                     "--output-format", "text",
                     "What exact word did I ask you to remember in our first message? "
                     "Reply with only that word.")
    verdict = classify_resume(returncode=answer.returncode, stdout=answer.stdout, token=token)
    return Result(
        name=M5_RESUME, status=MEASURED,
        data={"verdict": verdict, "transcript_was_compacted": compacted,
              "compaction_markers": markers, "transcript_bytes": transcript_bytes,
              "written_at": str(workspace), "resumed_at": str(elsewhere),
              "path_changed": str(workspace) != str(elsewhere),
              "ref": f"refs/grid/agent/{session_id}", "commit": head,
              "provider_gate_session_id": gate.session_id,
              "provider_gate_reason": gate.reason,
              "returncode": answer.returncode,
              "answer": answer.stdout.strip()[:500],
              "stderr": (answer.stderr or "").strip()[:500]})
