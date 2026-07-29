---
status: accepted
---

# The sweep kills by argv, never guesses an owner, and says what it was not shown

`grid leave`'s argv sweep decides what to terminate from **argv text alone** and reports on **only
what it was permitted to see**. [ADR 0020](./0020-process-identity-at-run-record-seams.md) gave the
*record* path an identity; the sweep deliberately has none — it matches by argv content precisely
because the records it backstops are the thing that has gone wrong. That leaves two questions this
ADR settles, because they were being re-litigated once per review gate: what the matcher may kill,
and what a scan that saw a fraction of the box is allowed to claim.

Found by issue 06's review gate, filed together as `.scratch/grid-leave/` issue 15 (PRD follow-up
F13) because fixing them separately would have traded the same thing off three times. Scope: **this
CLI only** — no wire value, no master or control-plane slice, nothing for the lockstep register in
`CLAUDE.md`.

Choices a future reader will otherwise re-litigate:

- **The matcher's discriminator is the argument count, and it is enough.** The spawn argv is
  `<cli> __remote-engine <network_id> <engine_id>` and `cli/_main.py` parses exactly two positionals,
  so a match requires the marker followed by *exactly* two tokens. That is what separates a serve
  child from a bystander that merely mentions it — an operator's own
  `pgrep -f "__remote-engine <nid>"`, a `watch`, a wrapper script — each of which has one token after
  the marker, and each of which the sweep force-killed before the guard existed (reproduced against a
  real process table). It is a lockstep coupling to the dispatch signature, noted at both ends.

  It is a *count* discriminator, not an identity check: a command line that happens to **end** in the
  exact `__remote-engine <nid> <engine_id>` shape still matches. Closing that residue needs an owner
  or executable check, which is the next bullet.

- **No owner filter. EPERM is the filter, and an elevated leave is authoritative on purpose.**
  Unelevated POSIX already declines to kill another user's process for free: `terminate_pid` does not
  catch EPERM, so the match lands in `foreign` and is reported, never signalled. Adding an explicit
  filter would buy very little and cost three things.

  It would make the case below **invisible** rather than reported — deleting the exact signal the
  `foreign` qualifier exists to raise. It would stop `sudo grid join` followed by an unprivileged
  `grid leave` from reaping anything, which is a real workflow rather than a hypothetical one. And on
  Windows it is not even cheap: `Win32_Process` exposes no owner property, so it means
  `Invoke-CimMethod GetOwner` per process — hundreds of WMI method calls inside a teardown that
  already has a 15s budget for the whole enumeration.

  The consequence is stated rather than hidden: run as root or Administrator, the sweep kills every
  process carrying **this grid's** marker regardless of owner. That is the intent — an elevated
  operator tearing down the box is not asking to spare half of it — and it is bounded by the marker,
  which names one grid.

- **A `foreign` match does not fail the leave, but it does qualify it.** `foreign` does not mean "an
  unrelated process". It means: a live process carrying this grid's exact marker and network id,
  which we could not stop. Exiting non-zero would be wrong — killing another operator's legitimate
  node is not ours to do — but the previous behaviour (a stderr note, then an unqualified
  `Left <grid>.` at exit 0) was wrong in the more expensive direction.

  The shape that makes it expensive is mundane. `sudo grid join` then an unprivileged `grid leave`
  over a shared `GRID_HOME` is the **same node_id**. What survives is the *process*, not the
  advertisement: the backstop's consumer flip stands, because the master's heartbeat writes only
  `last_heartbeat`/`load` and never `role`, and its pruned-row self-heal re-creates a `consumer` too.
  So the grid does stop listing the box — while the root-owned child keeps the engine, the port and
  the token it loaded at startup, and the operator was told the machine stopped doing work it is
  still doing. Worse if the backstop *degraded* rather than landed: the node was never flipped, and
  that child's heartbeats hold it inside the ~120s TTL as a provider for as long as it runs. So the
  success line carries the caveat and names an elevated retry, and exit 0 stands — which overturns
  the wording, not the decision, of issue 02.

  (An earlier draft of this paragraph said the child's next heartbeat *re-registers it as a
  provider*. It cannot: `ensure_node_exists` has self-healed a missing row as `consumer` since the
  master's first commit, so the 404 the client maps to `"missing"` is unreachable. Recorded rather
  than silently corrected, because the same sentence was copied into three other files and the
  correction is what a future reader needs to see.)
  `grid logout` inherits the same caveat as a warning and never as a refusal, for the second reason
  its sibling `_warn_unscanned` is a warning: a box hosting another user's node could otherwise never
  sign out at all.

- **On Windows, a partial scan was reported as a clean one, and no guard could have caught it.** WMI
  hands an unelevated caller a **null** command line for any process it cannot open. Those rows were
  filtered out of the enumerator's own output, so they reached neither the matcher nor any count —
  the sweep was structurally blind to another user's (or an elevated) serve child while `scanned`
  stayed `True`.

  The existing empty-output guard cannot close this, and the reason is in its own docstring: our own
  `powershell.exe` is always readable, so it always emits the one row that satisfies the guard. **A
  table that is 99% opaque passes it.** So unreadable processes are emitted as a sentinel row instead
  of dropped. A sentinel is inert by construction — it carries no marker, so nothing can ever be
  signalled because of one — and what it adds is a number: the size of this scan's blind spot.

- **The blind spot is compared to a floor, not to zero, and the floor is measured rather than
  guessed.** pid 0, pid 4, Registry, Memory Compression, Secure System and every protected process
  hand back a null command line to an **administrator** too, so qualifying on `unreadable > 0` would
  footnote every Windows leave ever run — and a caveat that is always printed is one nobody reads.

  The number that separates "opaque to everyone" from "opaque to this account" is the one thing only
  a real Windows box can supply, which is why issue 15/B could not be closed until the `test-windows`
  CI job existed. That job runs on a `windows-latest` runner **as an administrator** — i.e. against
  exactly the baseline the floor must clear — and asserts the real count stays under it, failing with
  the measured value. What that proves is one-directional: the floor is not too *low*, so no leave is
  footnoted for a table it could in fact see. That it is not too *high* is a hermetic boundary test's
  job, because no CI runner can produce a genuinely half-hidden table on demand.

- **Both Windows tools are named by absolute path, from the kernel.** `CreateProcess` resolves a bare
  executable name through a search order that includes the **current directory**, and a teardown runs
  wherever the operator is standing. The enumerator already did this for `powershell.exe`;
  `run_records.kill_group` was still invoking `taskkill` by name, with every Windows teardown in its
  blast radius. The root comes from `GetSystemWindowsDirectoryW` rather than `%SystemRoot%`, which is
  attacker-writable and survives UAC elevation — and which cannot be salvaged by validating the
  string, as three measured bypasses of the obvious regex show (a `subst`-mapped drive letter, U+017F
  case-folding to `s`, and a trailing newline that `$` matches before). It lives in `shared/` because
  `shared/` may not import `remote/` and both halves of a teardown need it.

Consequences. A `grid leave` that could not see the box, or found a live child of this grid it was
not allowed to stop, now says so on the line that reports success — the same class of honesty
`scanned=False` already had, extended to the two cases that looked identical to a clean run. Three
things are knowingly left open. **Windows pid recycling remains reachable through `taskkill /T`**,
which builds its tree from `ParentProcessId` — a field Windows does not invalidate when a parent dies
and its pid is recycled — so a process that lands on the recorded parent pid of a valuable process
and carries the marker can get that process killed as "a descendant"; Linux largely blocks the
analogue, since a pid in use as a pgid is not freed. That needs its own slice and is filed as PRD
follow-up F18. **`foreign` is still always empty on Windows** by the construction ADR-documented in
issue 06 — a visible-but-unkillable cross-user match becomes a `survivor` there, which fails the
leave loud — so the `foreign` caveat is POSIX-only in practice and the partial-scan caveat is its
Windows counterpart.

And the third is the floor's own price, which is worth stating flatly rather than leaving as a
corollary: **below the floor, a hidden count is indistinguishable from a clean scan.** A hidden
process cannot land in `foreign` either — `foreign` needs a successful argv match followed by EPERM at
kill time, and a sentinel is never a match — so the count is the *only* signal, and under 24 there is
no signal. That is a second, much narrower instance of the exact "couldn't check ≠ checked, clean"
failure the rest of this feature closes, accepted knowingly: the alternative is a caveat on every
Windows leave, which is a caveat nobody reads. The count is a statement about the **scan**, never
about serve children — it says how much of the table was hidden, never whether anything was hiding in
it — and calibrating the floor does not eliminate false negatives, it only bounds how many hidden
rows it takes to earn a warning.
