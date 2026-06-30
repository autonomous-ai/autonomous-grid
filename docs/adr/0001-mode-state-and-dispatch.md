# ADR 0001 — Mode state, `grid mode` / `grid use`, and mode-aware dispatch

Status: accepted (2026-06-27)

## Context

Grid is becoming one `grid` CLI with two **modes** — `local` (today's unauthenticated in-memory
proxy) and `remote` (a future thin client to autonomous's hosted relay).
This ADR records the foundational decisions for the persisted mode concept the rest of the
dual-mode CLI hangs off: where mode lives, how it is switched and overridden, how the active
grid is selected per mode, and how command handlers become mode-aware while remote is still a
stub.

Hard invariant: an existing local user with no state file must behave **exactly** as before.

## Decisions

1. **Mode-aware dispatch via a central table (`cli/dispatch.py`).** local handlers stay wired in the
   parser via `set_defaults(handler=…)`; a single dispatch layer resolves the effective mode once,
   stamps it on `args.mode`, and routes. Two explicit, fully-covering sets — `AGNOSTIC` and
   `REMOTE_HANDLERS` — classify every command; a test asserts the union covers the parser's commands
   (fail-loud, never fail-open). Remote entries are stubs now and become real handlers in later slices.

2. **`~/.grid/state.json`**, nested, default `local`, carrying a `version` for future migration:
   `{"version": 1, "mode": "local", "active": {"local": <name|null>, "remote": <name|null>}}`.
   A missing file ⇒ mode `local` and no active selection (today's `home`/sole-grid fallback). The
   mode/state kernel is shared infrastructure and lives in `shared/state.py`.

3. **`grid use <name>` sets the per-mode active grid**, consulted inside `local/config.py:select_grid()`
   so it applies to every grid-targeting command. Precedence: explicit positional `[grid]` > active
   selection > sole/`home` fallback. local validates the grid exists at set-time (`raise SystemExit`);
   a stored active that was later deleted is ignored at resolve-time (fall back, never crash).
   `grid use --none` clears; `grid use` with no argument prints the current active.

4. **Per-invocation override `--local` / `--remote` > persisted mode > default `local`.** The flags are
   stripped from `argv` before parsing so they work in any position; specifying both is an error.

5. **Remote is a clear stub this slice.** `grid mode remote` switches and persists (per the issue's
   acceptance criteria) with a one-line "not available yet" note. Mode-gated commands fail with a
   guiding `raise SystemExit` (non-zero exit, scripting-friendly) instead of running local code or
   crashing. Bare `grid` in remote mode shows the mode + active + how to switch, with no network calls.

6. **Command classification.** Mode-agnostic (run unchanged in both modes): `version`, `catalog`,
   `pull`, `rm`/`remove`, `engine *`, plus the new `mode` / `use` and bare `grid` (mode-aware
   display, but never gated). Mode-gated (remote → stub now): `up`, `down`, `ls`/`list`, `info`,
   `join`, `leave`, `models`, `engines`, `chat`, `image`, `edit`, `video`.

## Consequences

- local behavior is unchanged when `state.json` is absent; the existing test suite stays green.
- The dispatch table is the single seam later remote slices plug real handlers into.
- `select_grid()` becomes the one chokepoint where the active selection takes effect, so `chat` /
  `info` / `down` / `models` / `engines` / `join` / `leave` all honor `grid use` for free.
- A future local-only command added without classifying it in `AGNOSTIC`/`REMOTE_HANDLERS` fails the
  coverage test rather than silently running local code in remote mode.
- The override is matched as a bare token anywhere in `argv`; it is documented (not shown in
  per-subcommand `--help`). Acceptable on this surface.
