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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shared import shell

from . import claude_install, system
from .claude_install import BINARY, INSTALL_URL  # noqa: F401  (re-exported: the app's own identity)
from .target import GridSession

# Claude Code appends `/v1/messages` itself, so the base carries the relay prefix and **no** `/v1`.
# Leave `/v1` on — as `grid info --env` prints it for OpenAI clients — and every request 404s.
_RELAY_SUFFIX = "/relay"


def _require_live_models(session: GridSession) -> None:
    """Refuse a grid that is serving nothing at all — the whole of preflight now.

    ADR 0028 checked something much narrower: it injected a fixed model per Claude Code tier and
    refused a grid that did not serve those exact names. That table is gone (see ``_environment``),
    and with it any basis for this command to have an opinion about *which* model the app should ask
    for — so the only model fact still worth checking is the one that is true whatever the app asks:
    a grid with no live engines can serve nothing, and the launch would fail at the first prompt with
    an error naming a model rather than an empty grid.

    Still no off switch and still no question: the alternative is finding out inside Claude Code,
    where the error names neither the grid nor the way out.
    """
    if session.live_models:
        return
    raise SystemExit(
        f"Grid {session.label} serves no models yet, so Claude Code has nothing to talk to.\n"
        # `grid join` runs on the machine that will serve the model, and the launch is retried here —
        # neither command alone is the whole way out.
        f"Run `grid join` on a machine with an engine, then `grid launch {BINARY}` again."
    )


def _environment(session: GridSession) -> dict[str, str]:
    """The keys Claude Code is handed, layered over the inherited environment by ``run``.

    **No model variable is set, deliberately.** ADR 0028 injected seven — one per Claude Code tier,
    from a hardcoded table — so that the app would ask the grid for names the grid served. That put
    this command in charge of a choice it has no standing to make: which model a user's session runs
    on. Left unset, Claude Code resolves models the way it does everywhere else — its own defaults,
    the user's `settings.json`, and `/model` — and the grid answers for whatever it is asked. The
    cost is recorded where the decision is: a grid that does not serve what the app asks for now
    fails at the first prompt rather than at launch.

    `GRID_TOKEN` is absent for a different reason: it appears in the hand-rolled recipe, but it is a
    shell convenience variable this app never reads. Telemetry variables are absent too — whether
    Grid should disable Claude Code's error reporting is an open product question (ADR 0028), and
    leaving it out is how that stays a question instead of a silent default.
    """
    return {
        # `rstrip` before the suffix: a stored relay address with a trailing slash would otherwise
        # produce a doubled slash the client sends verbatim.
        "ANTHROPIC_BASE_URL": session.relay_base.rstrip("/") + _RELAY_SUFFIX,
        # Both, because which one Claude Code reads depends on the path it takes to authenticate.
        "ANTHROPIC_AUTH_TOKEN": session.access_token,
        "ANTHROPIC_API_KEY": session.access_token,
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

    def print_env(self, session: GridSession) -> int:
        """Print what ``run`` would inject, as shell exports, and start nothing.

        Preflight runs first and refuses on exactly the conditions a launch refuses on: exports for a
        grid that cannot serve them would move the failure into the app, which is the trap preflight
        exists to close.

        The app itself is deliberately **not** resolved here. Resolving it can offer to run the
        vendor's installer, and an installer is a spawn — this command's whole contract is that it
        starts nothing. A user printing exports is managing their own shell, where the binary may
        legitimately arrive later or under a name only they know.
        """
        _require_live_models(session)
        env = _environment(session)
        # Diagnostics on stderr, so stdout stays a block a shell can evaluate unfiltered.
        _warn_on_setting_overrides(session, env)
        # This prints the grid's access token, making `--print-env` the **second** deliberate
        # exception to "no command prints a token" (ADR 0003 §6). It carries the same justification
        # as `grid info --env` (cli/remote_grid.cmd_remote_info), the first: an explicit,
        # user-requested disclosure of the caller's own token to the caller's own shell, like
        # `gh auth token`. Every other launch path stays token-free.
        for key, value in env.items():
            print(f"export {key}={shell.quote(value)}")
        return 0

    def run(self, session: GridSession, argv: Sequence[str] = ()) -> int:
        # First, before anything touches the machine: can this grid serve anything at all? An empty
        # grid is a refusal here, not an API error at the first prompt.
        _require_live_models(session)
        # Resolved here rather than checked by the caller first: one call, no window in which the app
        # can leave PATH between a check and its use — which is also why `LaunchTarget` has no separate
        # installed-check member for the offer below to sit behind.
        binary = claude_install.resolve_or_install(self.label)
        env = _environment(session)
        _warn_on_setting_overrides(session, env)
        # One line, because after it the app owns the terminal and the user can no longer tell which
        # grid they are on. It names the grid and nothing else: this command no longer chooses a model
        # (see `_environment`), so naming one here would be a claim it cannot keep.
        #
        # stderr, not stdout: this is a diagnostic about the launcher, while stdout belongs to the app.
        # A user still sees it on a terminal, and a script capturing the app's output
        # (`grid launch claude -p … | jq`) gets that output unpolluted.
        # `flush` is load-bearing: the child inherits this stream, and block-buffered output would
        # otherwise hold the line until exit — printing it *after* everything the app wrote.
        print(
            f"Starting {self.label} on grid {session.label}.",
            file=sys.stderr,
            flush=True,
        )
        # The inherited environment with this grid's keys layered over it. `os.environ` is read, never
        # assigned to, so the CLI's own environment is untouched and no file is written.
        #
        # `argv` is appended unread (issue 05): it is the app's own command line, and the launcher
        # interpreting any of it would be a flag the app could no longer be given.
        return system.spawn([binary, *argv], {**os.environ, **env})


CLAUDE = ClaudeCode()
