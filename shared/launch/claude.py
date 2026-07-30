"""The `claude` launch target: Claude Code, pointed at a grid.

ADR 0028: everything the app needs arrives in **its own process environment** — never exported to the
user's shell, never written to a config file, never into this CLI's `os.environ`. Closing the app is
the entire cleanup, which is why there is nothing here to restore.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from . import system
from .target import GridSession

BINARY = "claude"

# Where a user without the app is sent. The vendor's own page, never a repackaged copy — and no pinned
# version anywhere, because Claude Code manages its own (ADR 0028).
INSTALL_URL = "https://claude.com/claude-code"

# The model each Claude Code tier resolves to on a grid. THE one constant: the single site a later
# discovery slice replaces, so no other module may name these models (a test enforces it). The
# `claude:*` family because those names map 1:1 onto Claude Code's own tiers — `/model haiku` inside
# the app then resolves to a model the grid actually serves, rather than to a real Anthropic model
# name no grid has ever heard of.
MODEL_TIERS = {
    "main": "claude:opus",
    "small_fast": "claude:haiku",
    "opus": "claude:opus",
    "sonnet": "claude:sonnet",
    "haiku": "claude:haiku",
    "fable": "claude:fable",
}

# The tiers every session uses: the main model, and the small/fast one the subagent tier mirrors. A
# grid that does not serve these cannot run a session at all, so their absence is a refusal. Every
# other tier is reachable only through `/model`, so its absence is a remap (below), not a blocker.
REQUIRED_TIERS = ("main", "small_fast")

# Claude Code appends `/v1/messages` itself, so the base carries the relay prefix and **no** `/v1`.
# Leave `/v1` on — as `grid info --env` prints it for OpenAI clients — and every request 404s.
_RELAY_SUFFIX = "/relay"


def _refuse_missing_required(session: GridSession, missing: list[str]) -> NoReturn:
    """Refuse a grid that cannot run a session, in a message that is actionable on its own.

    Both halves are load-bearing. Without the missing names the user cannot tell which tier failed;
    without what the grid *does* serve they cannot tell whether they mistyped a grid, joined the wrong
    engine, or never joined one — and they would have to run a second command to find out.
    """
    # Deduped: two tiers may want the same model, and naming it twice reads as two problems.
    wanted = ", ".join(dict.fromkeys(MODEL_TIERS[tier] for tier in missing))
    served = ", ".join(session.live_models)
    raise SystemExit(
        f"Grid {session.label} can't run Claude Code: it doesn't serve {wanted}.\n"
        + (f"Models on {session.label}: {served}.\n" if served
           else f"Grid {session.label} serves no models yet.\n")
        # Both commands named, because neither alone is the whole way out: `grid join` runs on the
        # machine that will serve the models, and the launch is retried here.
        + f"Run `grid join` on an engine that serves {wanted}, "
        f"then `grid launch {BINARY}` again."
    )


@dataclass(frozen=True)
class _Tiers:
    """What each Claude Code tier resolves to on one particular grid."""

    #: Every tier in ``MODEL_TIERS``, mapped to the model this grid will actually be asked for. Always
    #: complete — a remapped tier carries the main tier's model, never an empty string.
    models: dict[str, str]
    #: The optional tiers this grid does not serve, which now point at the main tier's model.
    remapped: tuple[str, ...]


def _resolve_tiers(session: GridSession) -> _Tiers:
    """What each tier will actually resolve to on this grid — or a refusal (ADR 0028).

    Preflight always runs: there is no flag that skips it and it never asks a question, because the
    alternative is discovering the answer inside Claude Code, where the error names a model and
    nothing else.
    """
    live = set(session.live_models)
    missing_required = [tier for tier in REQUIRED_TIERS if MODEL_TIERS[tier] not in live]
    if missing_required:
        _refuse_missing_required(session, missing_required)
    # Past the refusal, so the main tier's model is known to be live and is a safe destination for
    # every `/model`-only tier this grid happens not to serve.
    main = MODEL_TIERS["main"]
    models = {tier: (model if model in live else main) for tier, model in MODEL_TIERS.items()}
    return _Tiers(
        models=models,
        remapped=tuple(tier for tier, model in MODEL_TIERS.items() if model not in live),
    )


def _environment(session: GridSession, tiers: _Tiers) -> dict[str, str]:
    """The keys Claude Code is handed, layered over the inherited environment by ``run``.

    Every tier variable is always present, remapped or not: leaving one unset would send Claude Code
    back to a real Anthropic model name that no grid serves.

    `GRID_TOKEN` is absent deliberately: it appears in the hand-rolled recipe, but it is a shell
    convenience variable this app never reads. Telemetry variables are absent too — whether Grid
    should disable Claude Code's error reporting is an open product question (ADR 0028), and leaving
    it out is how that stays a question instead of a silent default.
    """
    model = tiers.models
    return {
        # `rstrip` before the suffix: a stored relay address with a trailing slash would otherwise
        # produce a doubled slash the client sends verbatim.
        "ANTHROPIC_BASE_URL": session.relay_base.rstrip("/") + _RELAY_SUFFIX,
        # Both, because which one Claude Code reads depends on the path it takes to authenticate.
        "ANTHROPIC_AUTH_TOKEN": session.access_token,
        "ANTHROPIC_API_KEY": session.access_token,
        "ANTHROPIC_MODEL": model["main"],
        "ANTHROPIC_SMALL_FAST_MODEL": model["small_fast"],
        "CLAUDE_CODE_SUBAGENT_MODEL": model["small_fast"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model["opus"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model["sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model["haiku"],
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model["fable"],
    }


# Claude Code's own configuration directory, overridable by this variable — the app's own contract,
# not ours. `~/.claude` is the default it falls back to.
_CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
_DEFAULT_CONFIG_DIR = ".claude"
_SETTINGS_FILES = ("settings.json", "settings.local.json")
# A settings file is a small JSON document. The cap is the second guard on an attacker-influenced
# read (see `_settings_env`): the regular-file check already rules out devices and FIFOs, and this
# bounds a merely enormous regular file. Generous by three orders of magnitude for real settings.
_MAX_SETTINGS_BYTES = 1024 * 1024


def _settings_paths() -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """The settings files whose ``env`` block outranks what we hand the child, and the locations that
    could not be worked out at all.

    The user's own settings first, then the project's — Claude Code reads both, and from inside the
    app a value from either looks identical, so both have to be checked or the warning is a
    half-truth. Deduped by path, because ``CLAUDE_CONFIG_DIR`` can legitimately point at the very
    directory the user is standing in.

    The two locations resolve **independently**, and neither may raise. This whole check is an
    ancillary nicety, reached only after preflight and the binary check have already decided the
    launch would succeed — so a location that cannot be resolved must cost the user a warning line,
    never the launch. Both failures are real: ``Path.cwd()`` raises once the shell's directory has
    been removed (a deleted worktree, a cleaned temp dir), and ``Path.home()`` raises on a container
    running as a UID with no passwd entry and no ``HOME``. Losing the cwd must still leave the
    user-level file checked, which is why these are not one ``try``.
    """
    paths: list[Path] = []
    unresolved: list[str] = []
    configured = os.environ.get(_CONFIG_DIR_VAR)
    try:
        user_dir = Path(configured) if configured else Path.home() / _DEFAULT_CONFIG_DIR
    except RuntimeError as exc:  # no HOME, and the UID has no home directory to fall back on
        unresolved.append(f"your home directory ({exc})")
    else:
        # Only `settings.json` at the user level: `settings.local.json` is a project-scoped file.
        paths.append(user_dir / _SETTINGS_FILES[0])
    try:
        project_dir = Path.cwd() / _DEFAULT_CONFIG_DIR
    except OSError as exc:
        unresolved.append(f"the current directory ({exc})")
    else:
        paths += [project_dir / name for name in _SETTINGS_FILES]
    return tuple(dict.fromkeys(paths)), tuple(unresolved)


def _settings_env(path: Path) -> tuple[dict[str, object], str | None]:
    """One settings file's ``env`` block, and why it could not be read.

    Exactly one of the two is meaningful: a missing file is ``({}, None)`` — the ordinary case, and
    silent. A file that exists but cannot be parsed returns the reason instead of pretending it was
    empty: "we could not check" is a different fact from "there is nothing to warn about", and a user
    whose settings are silently ignored by *both* Claude Code and this check deserves to know.

    Both guards below exist because this path is **attacker-influenced**: `grid launch` is run from
    inside whatever checkout the user is standing in, and git stores a symlink in four bytes, so a
    hostile repository can ship `.claude/settings.json` pointing anywhere. Reading `/dev/zero` never
    reaches EOF and opening a FIFO with no writer never returns — either would hang the launch before
    preflight ran. The *target* is what must be a regular file: symlinks stay allowed, because dotfile
    managers legitimately symlink these files and rejecting the link would break them for no gain.
    """
    try:
        # Follows the symlink on purpose — the question is what the path resolves *to*.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return {}, "not a regular file"
        with path.open("rb") as handle:
            # One byte past the cap, so "exactly at the cap" is still readable and anything larger is
            # detectable without holding the whole file.
            body = handle.read(_MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:  # includes a symlink whose target is gone: absent, not broken
        return {}, None
    except OSError as exc:
        return {}, str(exc)
    if len(body) > _MAX_SETTINGS_BYTES:
        return {}, f"larger than {_MAX_SETTINGS_BYTES} bytes"
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {}, str(exc)
    # A settings file whose `env` is absent or not an object simply sets no variables — that is
    # Claude Code's reading of it too, so it is not something to report.
    env = raw.get("env") if isinstance(raw, dict) else None
    return (env if isinstance(env, dict) else {}), None


def _warn_on_setting_overrides(session: GridSession, injected: Iterable[str]) -> None:
    """Warn — once — when the user's Claude Code settings would override what this launch injects.

    Reported, never repaired. Editing another tool's configuration file on a user's behalf is exactly
    the kind of side effect ADR 0028 exists to rule out: closing the app is meant to be the entire
    cleanup.
    """
    ours = set(injected)
    collisions: list[str] = []
    paths, unreadable = _settings_paths()
    unreadable = list(unreadable)
    for path in paths:
        env, unreadable_reason = _settings_env(path)
        if unreadable_reason is not None:
            unreadable.append(f"{path} ({unreadable_reason})")
            continue
        collisions += [f"{key} in {path}" for key in env if key in ours]
    if collisions:
        print(
            f"Warning: Claude Code's own settings set {'; '.join(collisions)} — settings override "
            f"what `grid launch` injects, so this session may not reach grid {session.label}.",
            file=sys.stderr,
            flush=True,
        )
    if unreadable:
        # Not silence: this check is the only thing standing between the user and a launch that looks
        # like a broken grid, so a check that could not run says so.
        print(
            f"Warning: couldn't read {'; '.join(unreadable)} to check for overriding "
            f"environment values; launching anyway.",
            file=sys.stderr,
            flush=True,
        )


@dataclass(frozen=True)
class ClaudeCode:
    """Claude Code as a launch target."""

    name: str = "claude"
    label: str = "Claude Code"

    def run(self, session: GridSession) -> int:
        # First, before anything touches the machine: will this session actually work? A grid that
        # cannot serve the required tiers is a refusal here, not an API error at the first prompt.
        tiers = _resolve_tiers(session)
        # Resolved here rather than checked by the caller first: one call, no window in which the app
        # can leave PATH between a check and its use. Issue 04 widens this to the two conventional
        # install locations and turns the refusal below into an offer to install.
        binary = system.find_executable(BINARY)
        if binary is None:
            raise SystemExit(
                f"{self.label} isn't installed, or isn't on your PATH (looked for {BINARY!r}). "
                f"Install it from {INSTALL_URL}, then run `grid launch {self.name}` again."
            )
        # After the binary check, so a machine without the app gets one clean error rather than a
        # preamble about tiers it will never use.
        if tiers.remapped:
            print(
                f"Grid {session.label} doesn't serve the {', '.join(tiers.remapped)} "
                f"tier{'s' if len(tiers.remapped) > 1 else ''} — `/model` there resolves to "
                f"{tiers.models['main']}.",
                file=sys.stderr,
                flush=True,
            )
        env = _environment(session, tiers)
        _warn_on_setting_overrides(session, env)
        # One line, printed by the target because only it knows which model it is about to ask for.
        # After this the app owns the terminal, and the user can no longer tell which grid they are on.
        #
        # stderr, not stdout: this is a diagnostic about the launcher, while stdout belongs to the app.
        # A user still sees it on a terminal, and a script capturing the app's output (once issue 05
        # forwards arguments, `grid launch claude -p … | jq`) gets that output unpolluted.
        # `flush` is load-bearing: the child inherits this stream, and block-buffered output would
        # otherwise hold the line until exit — printing it *after* everything the app wrote.
        print(
            f"Starting {self.label} on grid {session.label} (model {tiers.models['main']}).",
            file=sys.stderr,
            flush=True,
        )
        # The inherited environment with this grid's keys layered over it. `os.environ` is read, never
        # assigned to, so the CLI's own environment is untouched and no file is written.
        return system.spawn([binary], {**os.environ, **env})


CLAUDE = ClaudeCode()
