---
status: accepted
---

# No directory Grid creates under `GRID_HOME` is shared-writable

Run record *files* are force-`fchmod`ed to `0o600` (`shared/jsonio.atomic_write_bytes`), explicitly to
defeat the ambient umask, because a record is the on-disk handle to a live process. The **containing**
directory was not: `shared/paths.ensure_all`, `shared/paths.ensure_base`, `shared/jsonio`,
`shared/filelock`, `shared/engine/comfyui` and `cli/remote_provider` each created part of
`~/.grid/run/engines/<grid_id>/` with a bare `mkdir(parents=True, exist_ok=True)`, so its mode was
whatever the umask happened to be.

POSIX `unlink` checks write permission on the **containing directory** and consults nothing about the
target file's own mode. `0o600` on a record therefore never protected it from deletion. Under the
default `022` umask the directory lands `0755` and nothing fires; under a permissive umask it lands
`0777` and any local account can delete a record it cannot read.

What makes that worth a slice now is not the hole — it is pre-existing — but the **consequence**, which
[ADR 0025](./0025-local-leave-gets-a-second-net.md) changed. Before the argv sweep, deleting a record
only lost *tracking*: the engine kept running, untracked. Now a record-less child of that grid is
exactly what a bare `grid leave` reaps, so the deletion escalates to a kill — an attacker makes a
healthy engine *look* orphaned and the owner's own `grid leave` stops it. Note the shape: the kill is
issued by the victim's own process, so the EPERM boundary that would stop a cross-uid kill never
applies. Filed as `.scratch/grid-leave/` issue 19 (PRD follow-up F19) by issue 17's review gate, in
code issue 17 does not own.

## Decision

> **Wherever Grid creates a directory under `GRID_HOME` through `ensure_dir`, that directory is
> never group- or other-*writable* — at creation or afterwards. Nothing else about its mode is ever
> decided by us.**

The rule is stated for where the primitive runs, not as a claim about every `mkdir` in the codebase;
"What the rule covers today" below is the coverage claim, and it is deliberately narrower.

One rule, one primitive (`shared/paths.ensure_dir`):

| | behaviour |
|---|---|
| create | `os.mkdir(p, 0o755)`. The umask still applies on top and can only **tighten** (`umask 077` lands `0o700`). |
| repair | `mode & 0o022` → `chmod(mode & ~0o022)`. `0777`→`0755`; `0755` and `0700` untouched. |

### What the rule covers today, and what it does not

The rule above is the principle; this is its **reach as shipped**, stated because the difference is
not obvious and an unqualified claim here would be false. Six sites route through `ensure_dir`:
`paths.ensure_all`, `paths.ensure_base`, `jsonio.atomic_write_bytes`, `filelock._open_lock_fd`,
`engine/comfyui._write_pid_file`, and `remote_provider._spawn_remote_engine`. Between them they
cover the top-level tree (`~/.grid` and its `grids`/`bin`/`models`/`logs`/`run` children), the whole
**run tree** (`run/engines/<grid_id>/`), and the containing directory of every file written through
the hardened atomic writer — the run records, `state.json`, the grid configs, and the credential
stores.

They do **not** yet cover `~/.grid/services/` (ComfyUI's tree, `engine/comfyui.py` and
`models/media_bundles.py`), nor the install trees under `engine/installer.py`,
`agent/installer.py` and `agent/codex_installer.py`. Those keep their bare
`mkdir(parents=True, exist_ok=True)`. That gap is *not* smaller than the one closed here — it is
arguably larger, since `services/ComfyUI/custom_nodes/` holds Python that ComfyUI **imports** at
start-up and `bin/` holds binaries the CLI **launches**, so a shared-writable directory there is a
code-planting primitive rather than a record-deletion one. It is left out because it is a different
threat with a different blast radius, not because it is safe: extending to it is
`.scratch/grid-leave/` follow-up **F21**, and the swap is mechanical once that scope is agreed.

Choices a future reader will otherwise re-litigate:

- **Read/traverse bits are out of scope in *both* directions — this is the whole argument.** The
  obvious answer is `0o700`, matching the `0o600` files and the `grid_home().chmod(0o700)` that
  `remote/credentials.py` has always done after a sign-in. It was rejected on a mechanism, not a
  preference. A shared `GRID_HOME` across accounts is a topology the CLI already reckons with — a
  `sudo grid join` followed by an unprivileged `grid leave` is the shape behind
  [ADR 0024](./0024-what-the-argv-sweep-may-kill-and-what-it-can-see.md)'s `foreign` caveat — and
  forcing owner-only there takes *listing* away from the second account. `Path.glob` swallows the resulting `PermissionError` (CPython `pathlib`, the `scandir` call
  in `_select_from` is wrapped in `except OSError: return`), so `run_records.read_records` would answer
  a silent `{}` where today the `0o600` record fails loudly through `jsonio.load_json`
  (`SystemExit("Cannot read …")`). A silent `{}` is the **record-less-orphan fingerprint** — the exact
  signature this feature treats as an alarm, and what `known_grid_ids` already refuses to be quiet
  about. Hardening must not manufacture one. Confidentiality is already carried by the files; the only
  directory bit this threat turns on is `w`, and that is the only one taken.

- **Repair, not creation-only.** Creation alone would protect boxes joined after this ships — but a
  tree created under a permissive umask is precisely the one that is exposed, so the existing boxes
  are the case that needs fixing. Repair is idempotent, never widens, and never narrows read access,
  which is what makes it safe to run on every write.

- **`mkdir(0o755)` rather than `mkdir` + `chmod`.** Passing the mode to the syscall leaves no window in
  which the directory exists group-writable, and it is what lets the umask keep its say. A `chmod`
  afterwards would have had to choose a number, which is the decision the rule above declines to make.

- **The whole chain is walked, root-first.** `Path.mkdir(parents=True, mode=…)` cannot express this:
  CPython creates the *parents* with the default mode, so only the leaf would be covered and
  `run`/`run/engines` would keep whatever the umask gave them. `ensure_all` matters most here — it has
  thirteen callers (the installers, model and media-bundle downloads, ComfyUI, the media runtime), so
  on a real box `~/.grid` and `~/.grid/run` are routinely built by a `grid pull` long before any
  `grid join` writes a record.

- **Inert outside `GRID_HOME`.** A path that is not under `grid_home()` gets today's plain
  `mkdir(parents=True, exist_ok=True)` and no mode work: this is a rule about Grid's own tree, not a
  umask policy imposed on any path a caller happens to pass. The prefix test is literal
  (`Path.relative_to`), never `resolve()` — every real caller composes its path from `grid_home()`, and
  resolving would drag symlink semantics into a permissions decision.

## Consequences

`grid` no longer hands out the ability to delete another account's run records, on any box, whatever
umask the operator runs. The failure that ability now leads to — a healthy engine reaped by its own
owner's `grid leave` — is closed at the directory rather than papered over at the sweep, which is
correct: the sweep cannot tell a record that was never written from one that was deleted, and that
ambiguity is deliberate (it is what lets `grid leave` repair a genuine historical orphan).

Three residuals, and the first is reported rather than swallowed:

- **A directory owned by another account cannot be repaired.** The `chmod` raises EPERM and is
  non-fatal — best-effort, the same disposition `remote.credentials.save_credentials` gives its own
  `grid_home().chmod(0o700)`. An unprivileged `grid leave` over a root-created tree still has to reap
  its own serve child, and must not die on the way. A failed **`mkdir`** still raises: that caller
  genuinely cannot write. But non-fatal is not silent — it **prints a stderr note naming the
  directory**, because with nothing printed and nothing returned, a chain where one link could not
  be repaired is indistinguishable from one where every link was, in precisely the topology above.
  It stays rare by construction rather than by luck: a repair is only *attempted* on a directory that
  is already shared-writable, so a note means an exposure was found **and** could not be closed. The
  same note covers a `stat` that fails, where the mode could not even be read.
- **The repair `chmod` follows symlinks.** A relocated `run/` (an operator moving the tree to another
  disk) is repaired at its target, which is benign and probably wanted. A *planted* symlink is a
  redirect primitive — but only for an attacker who already has write access to the tree, i.e. one who
  can already delete the records this exists to protect. Not closed here; `chmod` has no `O_NOFOLLOW`
  equivalent on macOS's Python (`os.chmod(..., follow_symlinks=False)` raises `NotImplementedError`),
  so closing it means an `open(O_NOFOLLOW|O_DIRECTORY)` + `fchmod` walk, which is its own slice.
- **`~/.grid` itself stays `0755` for a local-only user.** Remote users already get `0700` from
  `remote/credentials.py` / `remote/api_keys.py` after any sign-in, which this generalises rather than
  replaces. The rule above deliberately does not force it, for the shared-`GRID_HOME` reason.

Windows is a no-op by construction: `mkdir`'s mode argument is ignored and `os.chmod` only toggles the
read-only bit, so the mode work is skipped outright — the same reasoning `jsonio.atomic_write_bytes`
already records for files, where per-user ACLs on `%USERPROFILE%\.grid` do the work instead.
