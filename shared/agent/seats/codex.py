"""Codex CLI adapter. Everything specific to the `codex` binary lives here.

Runs in its own CODEX_HOME (`~/.grid/seats/codex-cli`) with a config grid writes, so the
operator's hooks, skills and project trust are never loaded. Sign-in happens into that home:

    CODEX_HOME=~/.grid/seats/codex-cli codex login
"""
from __future__ import annotations

import json

from shared.agent.cli_seat import SeatError, SeatResult, SeatSpec

BINARY = "codex"
LABEL = "Codex"

# Exits 1 when not signed in.
SIGNIN_ARGV = ("login", "status")

# The seat runs in its OWN CODEX_HOME, so this config is the whole lockdown — the operator's
# hooks, skills and project trust live in their home and are never loaded here.
#
# Codex has no `--tools ""`: `shell` and `apply_patch` are core tools and cannot be removed. So
# execution is blocked rather than hidden — `approval_policy = "untrusted"` means every command
# needs an approval that `exec` has nobody to give, and `read-only` means none could write anyway.
#
# Consequence for benchmarking: unlike claude, the model still SEES shell/apply_patch and may try
# to call one instead of emitting `<tool_call>` text. A real difference between the CLIs, not a bug.
CONFIG_TOML = """# Written by grid at every seat start. Hand edits are overwritten.
# TOML: every bare key must come before the first [section] header, or it lands inside it.

approval_policy = "untrusted"
sandbox_mode = "read-only"
web_search = "disabled"

# Suppress the prompt blocks codex injects of its own accord. Verified with
# `codex debug prompt-input`: without these the model is handed a sandbox description, an
# "you are the primary agent in a team of agents" identity, a multi-agent note, and an
# environment_context carrying cwd, shell, date and timezone. With them it sees only the
# caller's message.
#
# Not cosmetic: the sandbox block made the model answer "I can't access that in this read-only
# workspace" to ordinary requests, and environment_context leaked the provider's shell and
# timezone. `[agents] enabled = false` is what removes the agent-identity block.
include_permissions_instructions = false
include_apps_instructions = false
include_collaboration_mode_instructions = false
include_environment_context = false

[agents]
enabled = false

[mcp_servers]

[skills]
include_instructions = false

[skills.bundled]
enabled = false

[tools.experimental_request_user_input]
enabled = false

# Every capability the model could otherwise reach. Verified by asking a live seat to list its
# tools: before this block it offered browser and computer control, and the whole
# `mcp__codex_apps__sites_*` family — deploy a site, change domains, read and write environment
# variables, mint bypass tokens, all on the operator's account.
#
# Scoped to CODEX_HOME=~/.grid/seats/codex-cli; the operator's own ~/.codex keeps every feature.
[features]
shell_tool = false
shell_snapshot = false
apps = false
computer_use = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
in_app_browser = false
image_generation = false
multi_agent = false
plugins = false
remote_plugin = false
plugin_sharing = false
hooks = false
skill_search = false
skill_mcp_dependency_install = false
goals = false
sqlite = false
code_mode_host = false
"""

# `--ignore-user-config` is deliberately ABSENT: the config above is ours and must be read.
# `--ignore-rules` stays — a project `.rules` file lives outside CODEX_HOME and would still load.
# `--disable shell_tool` is the one that matters: it REMOVES the tool, the way claude's
# `--tools ""` does. Verified with a canary file — before it, a request could `cat` any absolute
# path on the provider's disk; after it, the model reports no such tool exists. `-s read-only`
# and `approval_policy` alone did NOT stop this: read-only forbids writes, not reads.
LOCKDOWN = ["--ephemeral", "--ignore-rules", "--skip-git-repo-check", "-s", "read-only"]


def invoke(binary, prepared, tmpdir, stream=False):
    """`model_instructions_file` REPLACES codex's own base prompt with the caller's.

    Replacing, not prepending to stdin, for the same privacy reason as claude: the vendor's own
    prompt carries machine context. A file rather than `-c instructions="…"` because the value is
    parsed as TOML off argv, and a multi-line tool schema would need escaping and hit the argv limit.
    """
    argv = [
        binary, "exec",
        "--json",
        "-o", str(tmpdir / "last.txt"),
        "-m", prepared.model_alias,
    ]
    # Only replace codex's own prompt when the caller supplied one. Measured: codex's default
    # prompt reveals the cwd and nothing else — no username, no git, no OS — so with a scratch cwd
    # it discloses only a throwaway temp path. (Claude's default prompt is different: it carries
    # the real username, project name, git branch and OS, so that one is always replaced.)
    # Passing an empty instructions file is not an option either — codex refuses it outright.
    if prepared.system_prompt.strip():
        system_file = tmpdir / "instructions.md"
        system_file.write_text(prepared.system_prompt, encoding="utf-8")
        argv += ["-c", f"model_instructions_file={json.dumps(str(system_file))}"]
    argv += [*LOCKDOWN, "-"]  # "-" reads the prompt from stdin
    return argv, prepared.prompt


def parse_event(event):
    """Codex emits no text deltas — only the finished message on `item.completed`. So a streaming
    consumer gets one chunk, sent as soon as the event arrives rather than after the process exits.
    Time-to-first-token is therefore not measurable on this seat."""
    if event.get("type") != "item.completed":
        return None
    item = event.get("item") or {}
    return item.get("text") if item.get("type") == "agent_message" else None


def decode(proc, tmpdir):
    """Answer comes from the `-o` file; token usage from the `turn.completed` JSONL event.

    Codex reports tokens but no dollar cost, so `cost_usd` stays 0 — the seat reports what the
    CLI actually measured rather than estimating.
    """
    last_message = tmpdir / "last.txt"
    text = last_message.read_text(encoding="utf-8").strip() if last_message.exists() else ""

    usage, thread_id, failure = {}, "", ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # codex prints non-JSON lines too; they are not errors
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
        elif kind == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif kind in ("turn.failed", "error"):
            failure = str(event.get("message") or event.get("error") or kind)

    if not text:
        detail = failure or (proc.stderr or "").strip() or f"exit code {proc.returncode}"
        raise SeatError(f"`codex` produced no final message: {detail[:400]}")

    return SeatResult(
        text=text,
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cached_input_tokens") or 0)
        + int(usage.get("cache_write_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0)
        + int(usage.get("reasoning_output_tokens") or 0),
        cost_usd=0.0,  # codex reports no dollar figure
        session_id=thread_id,
        num_turns=1,
    )


SPEC = SeatSpec(
    kind="codex-cli",  # `codex` is taken by the OAuth HTTP seat (ADR 0015)
    binary=BINARY,
    home_env="CODEX_HOME",
    config_files=(("config.toml", CONFIG_TOML),),
    label=LABEL,
    signin_argv=SIGNIN_ARGV,
    login_argv=("login",),
    invoke=invoke,
    decode=decode,
    # No usage screen — the quota gate reads None and keeps serving.
    quota_argv=(),
    parse_usage=None,
    parse_event=parse_event,
)
