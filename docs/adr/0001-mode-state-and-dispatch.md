# ADR 0001 — Mode state, `grid mode` / `grid use`, and mode-aware dispatch

Status: accepted (2026-06-27); amended 2026-09-04 — the default mode for a machine with no
state file is now `remote`, derived rather than constant (D-2, D-4).

## Context

Grid is becoming one `grid` CLI with two **modes** — `local` (today's unauthenticated in-memory
proxy) and `remote` (a future thin client to autonomous's hosted relay).
This ADR records the foundational decisions for the persisted mode concept the rest of the
dual-mode CLI hangs off: where mode lives, how it is switched and overridden, how the active
grid is selected per mode, and how command handlers become mode-aware while remote is still a
stub.

Hard invariant: an existing local user with no state file must behave **exactly** as before.
This invariant **survives the 2026-09-04 amendment** — it is what that amendment is built around,
and it is now carried by evidence on disk rather than by the constant.

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

   > **Amended 2026-09-04 — the default is `remote`, and it is DERIVED, not a constant.**
   > A missing/invalid mode now resolves through `shared/state._default_mode()`: `remote` for a
   > new install, `local` when `~/.grid/grids/*/config.json` matches at least one grid. The
   > product wants a new user pointed at a hosted grid; the hard invariant above forbids taking a
   > working local grid out of an existing user's sight to get there, so the presence of a local
   > grid *is* the opt-out and no migration step or state write is needed.
   >
   > Three properties bind, each a way this has already been got wrong once in review:
   > - **`_normalized()` derives the same default.** `grid use <name>` writes `state.json`, so a
   >   plain `DEFAULT_MODE` there would persist `remote` for a local user as a *side effect* of
   >   selecting a grid — flipping them without a `grid mode` in sight.
   > - **The evidence is a `config.json`, never a bare directory.** A leftover empty
   >   `grids/<id>/` is not a grid.
   > - **Unreadable ⇒ `local`.** A new install has no `grids` directory and globs clean, so an
   >   `OSError` means the directory exists and only the read failed. "Cannot tell" must answer
   >   the way that leaves a user where they were.
   >
   > Accepted sharp edge: the default is *derived on every read*, never written, so a user who
   > removes their last local grid moves to `remote` — a mode change caused by an unrelated action.
   > It was preferred to the alternative (stamping `state.json` at first run), which trades this for
   > a write side effect on a machine the user has not configured yet. With no local grid left there
   > is nothing the flip can take away, and `grid mode local` is on the overview screen.
   >
   > `shared/state.GRID_CONFIG_FILE` hand-duplicates `local.config.CONFIG_FILE` — `local.config`
   > imports this module, so the dependency runs one way only and the kernel stays pure. Renamed
   > on one side alone, the glob matches nothing and every existing local user is silently moved
   > onto `remote`.

3. **`grid use <name>` sets the per-mode active grid**, consulted inside `local/config.py:select_grid()`
   so it applies to every grid-targeting command. Precedence: explicit positional `[grid]` > active
   selection > sole/`home` fallback. local validates the grid exists at set-time (`raise SystemExit`);
   a stored active that was later deleted is ignored at resolve-time (fall back, never crash).
   `grid use --none` clears; `grid use` with no argument prints the current active.

4. **Per-invocation override `--local` / `--remote` > persisted mode > derived default.** The flags are
   stripped from `argv` before parsing so they work in any position; specifying both is an error.
   *(Amended 2026-09-04: the tail of that chain was the constant `local`; it is now
   `_default_mode()` — see D-2.)*

5. **Remote is a clear stub this slice.** `grid mode remote` switches and persists (per the issue's
   acceptance criteria) with a one-line "not available yet" note. Mode-gated commands fail with a
   guiding `raise SystemExit` (non-zero exit, scripting-friendly) instead of running local code or
   crashing. Bare `grid` in remote mode shows the mode + active + how to switch, with no network calls.

6. **Command classification.** Mode-agnostic (run unchanged in both modes): `version`, `catalog`,
   `pull`, `rm`/`remove`, `engine *`, plus the new `mode` / `use` and bare `grid` (mode-aware
   display, but never gated). Mode-gated (remote → stub now): `start`, `stop`, `ls`/`list`, `info`,
   `join`, `leave`, `models`, `engines`, `chat`, `image`, `edit`, `video`.

## Consequences

- local behavior is unchanged when `state.json` is absent **and local grids exist on disk**; a
  machine with neither starts in `remote` (amended 2026-09-04).
- The dispatch table is the single seam later remote slices plug real handlers into.
- `select_grid()` becomes the one chokepoint where the active selection takes effect, so `chat` /
  `info` / `stop` / `models` / `engines` / `join` / `leave` all honor `grid use` for free.
- A future local-only command added without classifying it in `AGNOSTIC`/`REMOTE_HANDLERS` fails the
  coverage test rather than silently running local code in remote mode.
- The override is matched as a bare token anywhere in `argv`; it is documented (not shown in
  per-subcommand `--help`). Acceptable on this surface.
