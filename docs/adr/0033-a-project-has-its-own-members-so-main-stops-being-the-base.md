---
status: proposed
---

# A project has its own members, so `main` stops being the base

[ADR 0032](./0032-a-task-is-not-an-inference-transaction.md) built the task plane for one person
working alone. Three assumptions were true at once and each was load-bearing:

- a project belongs to exactly one owner, and is addressed as `(owner_id, name)`;
- at most one task in a project is active, enforced by a partial unique index;
- a project's repository is created by the relay and is written by nobody else, so its contents are
  a structural consequence of the code that writes them.

Three changes are wanted, and together they falsify all three assumptions:

- **several people work on one project**, with the rule "one active task *per person*" replacing
  "one active task per project";
- **an existing repository is imported**, `.git/` and all, rather than grown from an empty one;
- **`main` is a release branch**, so code an agent wrote and nobody reviewed must not land on it.

The last one is not an extra requirement bolted onto the first two. It is the one that forces the
rest, because ADR 0032 D-e gave `main` two jobs in the same sentence: *"`main` is always a known-good
state **and** always the base the project's next task builds on"*. Deciding that the first job needs
a human decision leaves the second job with no owner.

A note on words before anything else. **Member** already means "an email permitted on a grid,
carrying one or more roles", and **role** already means `provider` / `consumer` / `both`. This ADR
introduces a narrower fact — admission to one *project* — so it says **project member** and
**project role** throughout, never the bare words. Likewise the act of bringing `main` back into
someone's branch is **integrate**, not "sync": `grid sync` is the membership snapshot and has been
for far longer.

## What breaks first, and it is not merge conflicts

Relaxing "one active task per project" to "one per project member" looks like a one-line index
change. It is not, and the failure it produces is worth stating precisely because it is silent and
terminal.

Every task branch is cut from `main` **at create time** (`task_repo.commit_input`). Two tasks
running concurrently in one project therefore share a base. When the second one finishes,
`fast_forward_default` finds that `main` has moved, `merge-base --is-ancestor` fails, and the
terminal report is refused with 409 — leaving the task `running`. The reaper then reclaims it,
resets its branch back to the input commit, and hands it to another provider, whose attempt is cut
from the same stale base and fails the same way. The task burns every attempt it has and ends as
`retries_exhausted`, having succeeded every time.

That is not a conflict waiting to be merged. It is a loop, and the work is thrown away at the end
of it.

## The invariant is cited by name in four places, not one

`tasks_one_active_per_project` is not only an index. It is an invariant other code names in its own
comments in order to justify **not** checking something. An earlier draft of this ADR re-keyed the
index and asserted the rest followed; a review of the real code found three further sites, one of
them in the other repository, and one of them a claim this ADR itself made and got wrong. They are
listed here because the same mistake is available to anyone implementing it:

| Site | What it assumes | What the re-key does to it |
|---|---|---|
| `db.py` — the index | one active row per project | re-keyed to `(project_id, owner_id)` |
| `task_git._access` | `.scalars().first()` with no `ORDER BY`, over a comment reading *"At most one row can match … a unique index, not a convention"* | the row becomes arbitrary — see D-i |
| `remote/tasks._reserve_workspace` | keyed on `project_id` alone, over a docstring reading *"The relay's `tasks_one_active_per_project` unique index means this cannot happen"* | a provider refuses the concurrency the re-key enables — see D-g |
| integration (this ADR's own D-d) | that the index serializes integrate against a running task | **false for the tiers that write no row** — see D-d |

## Decision

> **`main` is a release branch that only a project member moves, and only through the relay. Every
> task is cut from, and lands on, its author's own WIP branch. Integration with `main` is an
> explicit act which always creates a task row, so it is serialized against that member's other
> work by the same index that limits them to one.**

Thirteen decisions follow. The last four (D-j through D-m) exist because this design is driven by an
application on behalf of a five-to-seven person team working on a repository that already has a
history — not by one person at a terminal starting from nothing. That changes which failures are rare
and which are daily.

### D-a — Project membership is the relay's own fact

A `project_members` table in the relay's database, not in the control plane. Project resolution
stops being `(owner_id, name)` and the git plane's fence stops reading `project.owner_id`.

The control plane already owns *grid* membership and syncs it to every relay
(`grid_auth.apply_sync_snapshot`), so putting project membership there would be the tidier-looking
choice. It is refused for the reason `tasks.py` already gives for refusing task scopes: a value the
relay needs but the control plane mints cannot be relied on until a control-plane release has gone
out, and until then every real caller is refused. Project membership is a fact about a table this
relay owns; it does not need a second repository's release cycle to become true.

The table carries a **project role** column from the first migration even though promotion does not
consult it. This is not speculative generality — `create_all` never ALTERs, so a column added later
is a hand-written migration on every live grid, which is precisely the reasoning `_claim_one`
already records for reading a session id out of `tasks` rather than adding a column to `projects`.

**It also carries a `member_key`, because `user_id` cannot be used as a name.** The relay mints
identity as `f"grid:{network_id}:{sub}"` (`grid_auth.py:217`) — a string containing **colons**. Git
forbids a colon anywhere in a ref name, and `_SAFE_PROJECT_ID` (`[A-Za-z0-9._-]+`) rejects one in a
path segment. So `wip/<user_id>` is not a ref that can be created and `<user_id>` is not a directory
that can be made: both fail on the first task, for every real user. An earlier draft of this ADR
wrote both without ever looking at what `user_id` holds.

`member_key` is derived once (`sha256(user_id)` truncated), stored, and is the **only** form that
appears in a ref name, a filesystem path, or the claim payload. Deriving it per call instead would
put the derivation rule in two repositories; storing it also means it survives a change in how the
control plane shapes `sub`. It preserves D-c's reason for not using an email — a name that travels
into every clone should not be a personal address — without depending on the token's contents.

**A project is addressed by id on the wire, not by name.** `_ensure_project(auth.user_id, name)`
resolves against `idx_projects_owner_name`, which is unique *per owner*, so a name is not an address
a second project member can use: posting `{"project": "acme"}` into someone else's project creates a
new, empty project of one's own — silently, today. A name also cannot be promoted into an address,
because a person belonging to two projects both called `acme` has no unambiguous one. So the client
sends a project id, `--project` accepts an id, and creating a project by name stays a separate,
explicit act by the person who owns it.

### D-b — Only a project member promotes to `main`, and only the relay writes it

Promotion is an endpoint (`POST /relay/v1/projects/{id}/promote`), fast-forward only, running the
`fast_forward_default` that already exists with the ref name as a parameter. A client never pushes
`main`.

Keeping the relay as `main`'s only writer is the whole reason a provider cannot announce its own
success (ADR 0032 D-e). Widening the git fence so members could push `main` directly would not just
add a second writer; it would remove the one sentence the branch-level fence rests on, and the
fence's existing rule (`is_owner and active is None`) is built on a project that is sometimes idle —
which a project with several people no longer is.

Fast-forward-only is not a limitation here, it is the mechanism: it makes "integrate, then promote"
the only possible order, which is exactly the order D-d wants. The cost is that `main`'s history is
linear and carries no record of who promoted what. That record goes to the task event log and the
database, which already exist, rather than to a merge commit that would exist only to hold it.

Two concurrent promotes are safe without further work — `update-ref <ref> <new> <old>` is a
compare-and-swap, so the loser raises rather than clobbering. What is *not* free is a promote that
lands inside another member's integration, between its read of `main` and its write of the WIP
branch: that integration's result then merges a `main` that has already moved, and its own promote
is refused as non-fast-forward.

For tiers 1 and 2 that window is seconds, and seven members colliding inside it is negligible. For
tier 3 it is **a whole agent run**, and the arithmetic is not negligible: with a merge taking `T` and
the team promoting at rate `R`, the chance of a collision is `1 − e^(−RT)` — 15% at one promote an
hour, **69% at seven**. Each retry is another slot-consuming, agent-running integration. A client
that promotes on every green task drives `R` up by exactly the factor that makes this bite, so the
promote refusal returns `main`'s current commit and the distance **as machine-readable fields**,
letting a client serialize its own promotes instead of discovering the rate empirically.

**Promote names its source ref, and any project member may promote any WIP branch.** Promoting only
"the caller's own" strands work permanently the moment someone leaves the team: nobody can promote
`wip/<departed>`, there is no adopt or transfer operation, and `_access` stops answering for them on
the next request. At five to seven people a departure is a quarterly certainty, not an edge case.
The project's members already read every WIP branch; letting them promote one adds no reach, and the
alternative is work reachable only by an operator with a shell on the relay.

### D-c — A task is cut from its author's WIP branch, which is created at `main`

Each project member has one ref per project, **`wip/<member_key>`** — the stored key from D-a, never
the raw `user_id`, which is not a legal ref name. A task is cut from it, and the relay fast-forwards
it when the task completes — the same mechanism `main` used, at a different ref.

**A WIP branch is created by pointing it at `main`, and a project with no trunk accepts no tasks.**
This rule is required rather than tidy. `commit_input` falls back to a **root commit** when its base
ref does not resolve, so a first task on a WIP branch that does not exist yet would produce a
history unrelated to `main` — after which that member's promote is refused forever, tier-1
integration can never fast-forward, and tier-2 sees every file as a conflict. The same outcome
reaches a fresh project from the other side, if two members create tasks before anyone has promoted:
two disjoint roots, permanently. So a project without `main` refuses task creation with a message
naming import or a first commit as the fix, rather than silently producing an orphan.

A derived key and not the member's email: a ref name travels into every clone of the repository, and
there is no reason for one person's address to be permanently recorded in another's working copy.

The term is **WIP branch**. `dev/` was rejected because "dev" already names the development VM
throughout this project's runbooks, and `base/` because `base_commit` already means something
else — the trunk tip a task was cut from.

**Every writer of a WIP branch is enumerated, and the settle-time check is kept.** The writers are:
task settle (fast-forward), integration (D-d), reaper reclaim (which resets the *task* branch, not
the WIP branch), and promote (which reads it and writes `main`). D-d serializes the first two
through the task table. What is deliberately **not** claimed is that the settle-time fast-forward
therefore cannot fail: the git write and the terminal database transaction are not one atomic act —
the transaction can still exhaust its retry loop into a 503, or the relay can die between them —
and a WIP branch left ahead of a task branch that is later reset produces exactly the terminal loop
this ADR opens by describing. The check stays, and a settle that cannot fast-forward is reported as
itself rather than as a generic 409.

**That state has no recovery path, and one has to exist.** Nothing can move a WIP branch backwards:
members do not push (D-h), promote writes only `main`, and there is no revert. Worse, the member's
*next* task is cut from that WIP branch, so a lost attempt's work silently becomes the base of the
next one — while the CLI prints, verbatim, *"Changes the lost attempt made in git are undone."* That
sentence was true under ADR 0032, where the reset covered the only ref that mattered. D-c makes it
false, so a relay-side reset of a WIP branch to a named commit ships with this decision rather than
being deferred with the other revert work.

### D-d — Integration is an explicit act, and every tier of it creates a task

Bringing `main` into a WIP branch happens when a project member asks for it. Not at task creation,
and not at the agent's discretion.

Automatic integration at create time was the tempting alternative and it fails in a way that is hard
to read: a task whose prompt says "add an endpoint" would fail because of somebody else's conflict,
and the failure would surface as that task's failure. It also silently answers D-e's question by
making the provider the merger in every case.

**Integration inserts a task row before it touches git — in every tier, including the ones that
never spawn an agent.** An earlier draft said the one-active-task index serialized integration, and
that was wrong: the index fires on an `INSERT` into `tasks`, so a fast-forward or a clean merge that
creates no row is not serialized by it at all. The ordering made it exactly backwards, because the
git write that moves the WIP branch would happen *before* the insert that was supposed to refuse it.
A member with a running task could then move the branch that task was cut from, and the task would
burn every attempt it had.

A read-then-check inside the integration handler is not the fix either. `create_task` already
refuses to rely on one — two concurrent creates both observe an idle project — and an integration
racing a task creation is the same shape. The row is what the constraint is made of, so the row is
inserted first and released when the integration finishes, whether or not an agent was involved.

### D-e — Conflict resolution is a task; everything simpler is not

Integration runs in three tiers, and only the last one involves a model:

1. **Fast-forward** — the relay's existing compare-and-swap. No agent, but still a task row (D-d).
2. **Clean three-way merge** — `git merge-tree --write-tree` in the bare repo, no worktree. Still
   deterministic git semantics; no model participates. (Requires git ≥ 2.38 on the relay host — an
   operational constraint, recorded here because nothing else in this repo depends on it.)
3. **Genuine conflict** — an ordinary task, run by a provider, whose agent resolves it.

The alternative for tier 3 was a model running on the client's own host, inside the grid. It is
refused for two reasons. A wrong merge does not announce itself: it compiles, the tests may still
pass, and it silently deletes one of two people's intent — so this is the point in the whole
pipeline where the *best* available model belongs, not the cheapest. And a client-side merge would
be a second path by which the repository's contents change, with its own fence, its own failure
modes and no durable log, against a design whose single organising rule is that everything touching
the repository goes through a lease.

The provider fetches `main` into the workspace **before** spawning the agent. `task_agent.child_env`
hands the child no grid credential at all, deliberately — *"the requesting user's token has no
business in this process"* — and that stays true: the agent merges refs that are already local.

**The relay creates the tier-3 task, not the client.** The relay is the party that ran the merge and
therefore the only one holding the verdict; a client that created the task would be acting on a
conclusion it did not compute, and `main` can move again between the answer and the request. One
call in, one verdict, no window. The merge task's owner is the caller, so D-d's row — and therefore
the index — covers it like any other.

The cost is that the relay now writes a prompt. It is a fixed template over two ref names, not
synthesis, and it is the same relay that already writes `task.terminal` and the task row itself —
but it is the first time relay-authored text reaches an agent, and that is worth naming rather than
discovering.

**The relay verifies that a merge task actually merged.** `settled_result_commit` asks only whether
the branch tip matches what the provider reported; for a merge task that is not enough. An agent can
resolve a conflict by discarding `main` wholesale, or commit nothing at all — `commit_and_push`
passes `--allow-empty` — and the task would report `completed`, the WIP branch would fast-forward to
a commit that does not reach `main`, integration would report success, and the member's next promote
would be refused with `does not fast-forward` and nothing anywhere saying why. So a merge task's
settle additionally requires `merge-base --is-ancestor <merged-ref> <result_commit>`, and a result
that fails it is a failed integration, not a completed one. The relay is the only party that can
make this check, being the only one holding the verdict.

That check is **terminal, not a 409**. A 409 leaves the row `running`, so the reaper reclaims it at
the lease TTL, resets the branch to the same input commit, and every attempt fails identically —
spending the whole `max_attempts` budget on a cause no retry can change and ending at
`retries_exhausted`, which does not even carry the real reason.

**What the relay proves is that the merge HAPPENED, not that it is right — and the other half of
that is the provider's, because the relay structurally cannot see it.** `commit_and_push` runs
`git add -A`, and (measured, git 2.54.0) `add -A` clears a conflicted index to zero unmerged
entries: the conflict markers are staged as if they were a resolution, `git commit` succeeds where
git itself would have refused, and what comes out is a structurally perfect two-parent merge commit
that passes the ancestry check. The index is never pushed, so only the provider ever sees it. It
therefore reads `git ls-files --unmerged` **before** `add -A` and fails the task naming those paths,
while still committing and pushing so the work stays readable (D-e's own rule, and ADR 0032 D-e's).

**What counts as unresolved is git's index, never the file's contents.** An earlier draft of this
decision said a path counted only when it was unmerged *and* still carried `<<<<<<<` markers, on the
reasoning that an unmerged index means merely "not `git add`ed". A **modify/delete** conflict refutes
that: one side deletes a file, the other edits it, and git leaves **no markers at all** — measured on
2.54.0, it writes the surviving side's content verbatim and reports the conflict only through the
index and its exit status. An agent that did nothing then produced a structurally perfect two-parent
commit that passed the ancestry check above, and the deletion one member intended was discarded in
silence — this decision's own failure, reached through a conflict class the marker test could not
see. Every non-textual conflict (rename/rename, add/add of a binary, mode changes) was invisible the
same way.

So the merge prompt requires `git add` — or `git rm` — on every conflicted path, and any path still
unmerged when the agent stops fails the integration. The accepted cost is stated rather than hidden:
an agent that resolves a file and stages nothing has its task failed and one run is wasted, with its
work still pushed for the member to read. That is the cheap side of a trade whose other side is
somebody's deletion silently discarded, permanently, with every signal reading healthy.

A check that could not RUN is carried as its own fact rather than as an empty result, and disclosed
to the task's own event log: the task still completes — turning a git blip into a lost push would be
worse — but "this merge was verified" and "nobody looked at this merge" must not be the same
observation.

Neither check catches a merge that is complete and *wrong*, which is the same class as an agent
writing wrong code, and `docs/cli.md` says so rather than leaving it to be discovered.

**The parentage is structural, whichever way the agent finishes.** If it commits the merge itself the
merge commit is its own; if it resolves and leaves `MERGE_HEAD` in place, the provider's own
`git commit` consumes it and produces the two-parent commit. Both make the ancestry check pass, and
neither passes when no merge happened — so the check does not depend on the agent having phrased a
git command correctly. Measured on git 2.54.0 and pinned by a test in each repo.

**The claim payload gains the ref to merge from.** An ordinary task needs only its own branch; a
merge task needs the provider to fetch a second ref before spawning, and the agent cannot fetch it
itself. Sending the ref rather than a task *kind* keeps the `wip/` and `main` literals in one
repository, and degrades correctly: *absent ⇒ the provider merges nothing*, which is exactly the
pre-integration behaviour, so an old provider that claims a merge task runs it as an ordinary task
and reports a result that changes nothing, rather than failing in a new way.

**What the provider fetches and what settle checks are pinned to a commit, not to `main`.** These
look like one value and are two, and conflating them destroys correct work. The provider needs a
*name* to fetch — `uploadpack.allowAnySHA1InWant` is off, which D-f depends on, so a bare object id
is unfetchable. But a name **re-resolves at settle**: member A integrates when `main` is at M1, the
agent merges for twelve minutes, member B promotes at minute five, and settle resolves `main` to M2
and refuses a merge that was entirely correct.

At team scale that is not a rare interleaving. With merge duration `T` and promote rate `R` the
collision chance is `1 − e^(−RT)`: at seven members promoting once an hour and a ten-minute merge it
is **69%**, giving an expected 3.2 attempts against a `max_attempts` of 3 — so the *expected* outcome
of a conflicting integration becomes `retries_exhausted` on work that merged correctly every time.

So integration resolves `main` **once**, records that commit, and publishes it under a relay-authored
ref (`refs/integrate/<task_id>`) which D-i's allowlist un-hides for that task's lease holder alone.
The provider fetches the pinned ref; settle checks ancestry against the recorded commit.

**"Lease holder", not "provider", and the distinction cost a release.** D-i's allowlist has an arm
for a provider and an arm for a member, and one caller is frequently **both** — a small team runs
its own provider, so the account holding the lease is also a project member. Written as two
descriptions, the member arm was implemented as an early return: wider for branches, narrower for
`refs/integrate/*`, which lives outside `refs/heads/` precisely so that no member clones it. The
result was that tier-3 integration could not complete at all on that topology. The two arms are a
**union**, never a choice; anything added to one has to be answered for in the other. `main` moving
during a merge is then simply a promote the member has to integrate again — a second round, not a
destroyed one.

### D-f — An imported repository is validated before it becomes `main`

Import pushes to a staging ref (`refs/import/<id>`). The relay walks the reachable object graph and
only then sets `main`; a repository that fails validation leaves the project with no trunk rather
than a bad one.

This is where ADR 0032's strongest guarantee is deliberately traded down. Today "this repository
cannot contain a symlink or a submodule" is **structural**: the relay writes the mode literally as
`100644` and has no other code path. A packfile carries whatever it carries, so after import the
guarantee has to be carried by validation rather than by construction.

**Claude Code's own project-scope config is code execution, it is reachable today, and the binary's
one protection against it is disabled by how the provider invokes it.** Measured, not reasoned:

- `claude --help` on 2.1.x says of `-p/--print` — *"The workspace trust dialog is **skipped** when
  Claude is run in non-interactive mode (via `-p`, or when stdout is not a TTY, e.g. piped or
  redirected output). **Only use this in directories you trust.**"* The provider's argv is exactly
  `-p … --output-format stream-json` with stdout piped, so the dialog never appears — and the
  directory is, by construction, a tree that arrived over the wire.
- Run with the provider's exact invocation shape in a directory holding a `.claude/settings.json`
  with a `SessionStart` hook, **the hook executes** — before the model decides anything, with no
  permission prompt, as the provider's own user with its real `HOME`.
- Adding **`--setting-sources user`** blocks it. **`--strict-mcp-config`** does the same for
  `.mcp.json`.

`task_files._RESERVED_COMPONENTS` is `{".git", ".grid"}`, so `grid task create --file` accepts
`.claude/settings.json` **today**, without import and without multi-membership. This is the class ADR
0032 D-b already names — *"a file committed to `.git/hooks/` executes on the provider at checkout"* —
under a different filename. What multi-membership adds is escalation: one member promotes such a
file, everyone integrates, and it runs on every provider under every other member's tasks.

Two independent fixes, and **the provider-side one is load-bearing**:

1. The agent is spawned with `--setting-sources user --strict-mcp-config`. One change, covering every
   repository including imported ones, independent of any validator guessing filenames correctly.
   `user` rather than nothing: user-scope settings live in the operator's own `CLAUDE_CONFIG_DIR` on
   the operator's own machine. It is the *repository's* settings that must not load.
2. The **upload** path refuses `.claude/` and `.mcp.json` outright.

The two paths are deliberately asymmetric, and an earlier draft of this ADR got that wrong by asking
the import validator to refuse `.claude/` as well. It must not: real repositories commit `.claude/`
— this one does — and a `CLAUDE.md` in an imported repository is the normal, wanted case, not an
attack. Which is the general rule for the *instruction* class (`CLAUDE.md`, `.claude/agents/`,
`.claude/skills/`, `.claude/commands/`, plugins): it cannot be banned and is not trying to be. Its
trust level is exactly that of the source code the agent was asked to modify, which this design
already accepts. The line being drawn is narrower and firmer — **a shell command must not run before
the model has said anything.**

There is a bigger hammer (`CLAUDE_CODE_SIMPLE=1`) that also disables `CLAUDE.md` discovery, and it is
unusable here: its help text states auth becomes *"strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` —
OAuth and keychain are never read"*, and the provider's subscription is exactly OAuth.

> **Correction, 2026-08-06 (issue 23).** The paragraph above implies that `--setting-sources user`
> leaves `CLAUDE.md` discovery alone and only the bigger hammer takes it away. **It does not.**
> Measured on 2.1.223 with a prompt that forbids tools, so that only auto-loaded context can answer:
> with `--setting-sources user` the model replies `UNKNOWN`, and with no `--setting-sources` at all
> it answers from the workspace's `CLAUDE.md`. The original measurement was watching the model open
> the file with the `Read` tool, which is why issue 22's own test for this was intermittently red.
>
> What survives unchanged is the decision and everything it rests on: the *execution* class must not
> load, the flag is what stops it, and the import validator must still not refuse `.claude/`. What is
> wrong is the claim about the *instruction* class. Corrected: a repository's instructions stay
> **readable** — they are on disk in the workspace and an agent that looks finds them — but they are
> **not loaded into the model's context**. A task prompt that depends on a repository's conventions
> has to say so.
>
> The flag stays. Recovering automatic discovery means loading the repository's settings again, which
> is the hole this decision exists to close; the option that keeps it closed is reading the
> workspace's `CLAUDE.md` and passing it through `--append-system-prompt`, and that is a decision for
> whoever picks it up rather than a side effect of issue 23.

Related, and inherited without re-examination: `_require_provider` checks only that the caller is a
provider **on the grid**, so any registered provider node may claim any task in any project. ADR
0032 bought that with its "internally operated fleet" assumption, when every repository was one the
relay had grown from empty. Import makes the same sentence mean "any provider may receive a
checkout of any imported company repository", and that is a bigger claim than the one that was
agreed. It is not resolved here; it is named so it is decided deliberately rather than inherited.

> **Decided, 2026-08-08 (issue 24).** It ships as a **domain allowlist**: provider and client must
> belong to the same company, and a provider only serves tasks created by a client in that domain.
> This is a **release gate for import**, not a later refinement — until it lands, the sentence above
> describes the shipped behaviour. The relay has no domain concept today; `UserRow.email` is where
> one is derivable from, with two traps recorded in the issue (the `@unknown` synthesized address in
> `apply_sync_snapshot`, and the fact that most authenticated paths never write a `UserRow` at all).
>
> **As built.** `TASK_SERVED_DOMAINS`, a comma-separated list on the relay's own config, parsed and
> enforced by `task_domains.py`; grid-apis is untouched. The rule is **membership in the list for
> both sides**, not equality between them — two people on `gmail.com` are not one company, so
> equality would authorize strangers on any consumer mail host to each other's repositories. With
> the single entry that "provider and client same company" describes, the two coincide.
>
> **Empty is the off switch**, and that is what makes the gate shippable rather than a flag day: a
> `users` row is written only on the GRID-token path, so a gate demanding a resolvable domain
> unconditionally would refuse every task in both unit suites, in the cross-repo E2E
> (`GRID_MODE=false`), and in any non-Grid deployment. Unset ⇒ the claim query is byte-identical to
> the pre-24 one. Set ⇒ the gate fails **closed**: an unresolvable domain is refused, never waved
> through. That refusal is safe because in Grid mode both sides always resolve —
> `grid_auth._upsert_identity` writes the `UserRow` and the `NodeRow` bound to it on the caller's
> first authenticated request.
>
> Trap 1 closes structurally: an unresolvable domain is `None`, `None` is not in the list, so
> `@unknown` matches nobody *including another `@unknown`* with no special case — and
> `task_domains.validate_config()` refuses to boot on an allowlist containing the sentinel, so that
> holds against a typo as well as against convention.
>
> Three enforcement points, not one. The filter is **in the claim SELECT** rather than after it,
> because `attempt` is incremented by the guarded UPDATE (popping a task and putting it back spends
> a real attempt on a provider never allowed to run it) and the candidate SELECT is capped (a filter
> applied afterwards starves a provider whose eligible task sits past the cap). `_require_provider`
> refuses a provider whose *own* owner is unserved, before the long poll, so a misconfigured fleet
> does not look like an idle one. And `task_git._access` narrows the tasks a caller holds a lease
> on — the claim filter structurally cannot cover a task already `running` when the relay restarts
> into this code, and that lease is a checkout of the whole repository.
>
> A task in another domain is **invisible** (204, the 404-not-403 rule); only the provider's own
> ineligibility is a refusal, because that discloses nothing and is the one thing that can explain a
> permanently empty queue.

The validator refuses submodules (`160000`) outright, and refuses any path under `.grid/` — closing
on the push path the hole `task_files` already closes on the upload path. Symlinks (`120000`) are
allowed when their target stays inside the repository and refused when it escapes: refusing all of
them would reject ordinary repositories, and allowing all of them would leave the exact route ADR
0032 D-b names — a link into the provider's config directory, whose credential then reaches the
transcript, which is committed back to the requesting user's repository.

**The provider's `-c core.symlinks=false -c core.hooksPath=…` is NOT the second layer it was taken
for.** Those flags are set on the *provider's own* git invocations. The agent is handed
`dict(os.environ)` plus at most `CLAUDE_CONFIG_DIR` — no `GIT_CONFIG_*`, and the operator's real
`HOME` — it runs with `--permission-mode bypassPermissions`, and D-e above *requires* it to run
`git merge`. So the moment the agent touches git in an imported repository, a `120000` object
materializes as a real symlink and the operator's personal `~/.gitconfig`, `core.hooksPath` included,
applies to a tree that arrived over the wire. The validator is therefore not a second layer behind a
first one; for anything the agent does it is the **only** layer, and the provider's environment has
to be closed separately (a `GIT_CONFIG_*` floor in `child_env`, and `core.symlinks=false` written
into the workspace's own config as well as onto each invocation) before import is offered.

Git LFS is **not** supported and the validator says so rather than failing later: the agent has no
credential to fetch LFS objects, so it would find pointer files and might edit them as though they
were content.

**Validation runs after `receive-pack` has already moved the staging ref.** There is no quarantine
to lean on: `ensure_repo` creates the repository with `--template=`, so there is no `pre-receive`
hook, so git sets up no `GIT_QUARANTINE_PATH`. The objects land and the ref moves, and only then is
the graph walked. That is safe only because `refs/import/*` is hidden from every provider by D-i's
allowlist and `uploadpack.allowAnySHA1InWant` is off — so an unvalidated object is unreachable by
anyone — and it is stated here because "walk the graph after receive-pack" otherwise reads as though
the ref were still pending.

### D-g — A workspace, and therefore a conversation, belongs to a (project, member) pair

The workspace moves from `/var/grid/projects/<project_id>/workspace` to
`/var/grid/projects/<project_id>/<member_key>/workspace`, and the committed transcript from
`.grid/agent/` to `.grid/agent/<member_key>/` — the D-a key again, because `user_id` fails
`_SAFE_PROJECT_ID` for the same reason it fails as a ref name.

One change, because in this design they are one thing: Claude Code derives a session's transcript
directory from the working directory, so the cwd *is* the conversation's identity. Three separate
failures close together:

- two members' tasks landing on the same provider would share one directory, and `materialize`
  begins with `reset --hard` and `clean -ffdx`;
- `_claim_one` resolves the resume session per *project*, so one member would continue another's
  conversation;
- both would commit an append-only JSONL transcript to the same path, which conflicts on every
  single integration, and is the last thing anyone wants an agent resolving.

ADR 0032's constraint is preserved exactly as stated: the absolute path must be identical on every
provider, or `--resume` cannot find the session. It is now identical at one level deeper.

**The provider's own workspace reservation must be re-keyed with it.** `_reserve_workspace` takes
`project_id` alone, and its docstring justifies that with the very index this ADR re-keys. Left
alone it refuses the second concurrent task in a project — and refuses it by design with *no
terminal report*, so the task sits `running` for a lease TTL, is reclaimed, and can be refused
again, reaching `retries_exhausted` on a provider that had capacity the whole time. This lives in
the other repository, which is exactly why it was missed once already.

**What this does not close is cross-member reading of transcripts.** Per-member workspaces isolate
the live directory and the resume, but `.grid/agent/<member_key>/` is *committed*, so after one promote
and one integration every member's transcript is a tracked file in every other member's working
tree — readable by an agent running with `bypassPermissions`. Only the resume is isolated; the
content is shared by construction, because sharing it through the ordinary result commit is what
makes cross-provider resume work at all. A project's members can read each other's conversations,
and that is a property of the design rather than an oversight.

The cost in size is that the transcript growth ADR 0032 accepted (105 KB–2 MB per task) is
multiplied by the number of project members.

### D-h — The client's credential never lands on disk

`grid project clone` clones and configures a git credential helper that calls the CLI back, instead
of printing a URL for the member to wire up themselves.

`grid task fetch` avoids writing the token into `.git/config` today, and the reason is recorded at
its call site: a result directory is something people zip up and pass around, and a grid access
token lives a year. That reasoning does not survive a member who has a real clone and runs
`git pull` from an IDE — unless git can ask for the credential each time. The helper also means a
refreshed token is used automatically, where a token written once is a token that expires in place.

**A member may not push their WIP branch.** The obvious next action inside a real clone is
`git push`, and allowing it would add a writer of the WIP branch that the task table cannot see — a
push never inserts a row, so D-d's serialization cannot reach it, and a push that fast-forwards
`wip/<self>` while that member's own task is running breaks the task's settle exactly as an
unserialized integration would. `receive.denyNonFastForwards` prevents destruction but not this.
Today's equivalent right is gated on the project being idle, and a project with several members
rarely is. So the WIP branch is written by the relay alone, like `main`; a member who wants to
resolve a conflict by hand does it in their clone and hands the result to an integration, rather
than pushing past it.

### D-i — The git fence answers with a set, not a row

`_access` selects the project's active task with `.scalars().first()` and no `ORDER BY`, under a
comment stating that at most one row can match because the index is not a convention. The re-key
makes that comment false and the row arbitrary — and the consequences are not cosmetic. A provider
holding the lease on one member's task can be handed another member's row, decide it holds no lease,
and receive **404 on its own fetch and its own result push**, non-deterministically, per request.

`GRID_MAX_TASKS` compounds it: a provider runs several task workers, so one node can legitimately
hold two leases in one project at once. Every phrasing of the fence in the singular — "its own task
branch", "which single ref each may write" — is unable to express that. So the fence resolves the
caller's active tasks as a **set**, keyed on `provider_id`, and returns the union: the task branches
it may write and the WIP branches of those tasks' authors.

**`transfer.hideRefs` becomes an allowlist**, not an extension of the current denylist. Hiding
`refs/heads/task` and un-hiding one entry is complete only while the relay authors every ref in the
repository; an imported repository brings arbitrary branches and tags, and a denylist shows a
provider all of them. Written as "hide `refs/`, then un-hide `main`, the author's WIP branch, and
the task's own branch", verified on git 2.54.0 against a repository built exactly as `ensure_repo`
builds one: multiple `-c transfer.hideRefs` values accumulate in order, the hidden refs disappear
from both `upload-pack` and `receive-pack`, and a push to a hidden ref is refused server-side even
when the ref does not yet exist.

The allowlist necessarily hides `refs/tags/*` from providers, which is a real cost on an imported
repository — `git describe` and any version stamping in a build stop working inside a task. It is
accepted rather than solved, and named here so it is not rediscovered as a bug.

### D-j — A member can put a change in without running an agent

`POST /relay/v1/projects/{id}/commit` takes files through the existing `task_files` validator,
commits them onto the caller's WIP branch, and inserts and releases a task row exactly as an
integration tier 1 does — same serialization, no agent, no provider.

Without it the design has no path at all from "I edited a file on my machine" to "it is in the
project" except creating a task. D-h forbids pushing, promote only fast-forwards `main` from a WIP
branch, and integration takes no payload. So the cheapest way to fix one line the agent got wrong
would be a whole-file upload that spends the member's one slot and then **runs an agent that may
change the very line being fixed**. At five to seven developers, "the agent got it 90% right, let me
fix the last line" is the most common action of the day, and answering it with a paid agent run is
the design failing at its most frequent case rather than its hardest one.

This is deliberately not a relaxation of D-h. The write still goes through the relay, still lands on
one ref, and still holds a task row while it does — so the serialization that D-d exists to provide
is unchanged. What it removes is the agent, not the fence.

Two properties of `commit_input` have to change with it, and both were measured on git 2.54.0 rather
than reasoned about:

**Deletion is expressible only one way.** `update-index --force-remove` requires a work tree, which
the relay never has; `update-index -z --index-info` fed a zero mode removes an entry in the same
stream as the additions. So an explicit delete list rides the existing call, and it inherits the
existing path validator — otherwise it is a new route into `.grid/`, where deleting another member's
transcript directory destroys their conversation. Git no-ops silently on a path that is not there,
so the relay checks the base tree itself: for a delete, "succeeded, did nothing" is the wrong
direction to fail.

**The mode guarantee is that the server chooses it, not that there is one of them.**
`update-index --index-info` accepts a `120000` line and creates a real symlink entry, so what makes
a symlink impossible is that no field on the wire can carry a mode — not that `FILE_MODE` is a
single constant. It can therefore become a closed two-value mapping (`100644` / `100755`) driven by
a **boolean**, with the default read from the base tree, without weakening anything. That closes a
measured defect: re-uploading an existing `100755` file rewrites it to `100644`, so editing one line
of a shell script silently removes its executable bit. Dormant today, because the relay has only
ever written one mode; live the moment D-f imports a repository that has another.

### D-k — The queue is not the run

`deadline_at` is set at create and `reap_past_deadline` covers `queued`, so a task's whole wall-clock
budget starts before any provider has looked at it. That was correct for one member: the reaper's own
docstring says why — a `queued` task on a grid with no provider would otherwise hold its project's
lock forever.

At team scale it converts a capacity shortfall into silent data loss. Task workers default to one per
provider, so three provider machines serve roughly nine twenty-minute tasks an hour; seven members at
two tasks an hour demand fourteen. The queue grows by five an hour, crosses the one-hour deadline in
about ninety minutes, and after that **every task created is reaped having never run**, with
`attempt = 0`. Nothing distinguishes it from a task that ran and hung — both are `deadline_exceeded`
— and a client following a queued task past its deadline gets a 410 from the event stream while the
task record still says `queued`.

So the two budgets separate: a queue TTL that bounds waiting, and a run deadline that starts at
**claim**. The terminal reason distinguishes "never picked up" from "ran too long", because they are
the same word today and they call for opposite actions — add providers, or fix the task.

**As built (issue 18).** Four things the decision left open are answered here rather than inherited.

- **One column, re-anchored — not two deadlines.** `deadline_at` keeps its name and gains a stable
  meaning: *when this task ends if nothing changes*. It is written `created_at + queue budget` at
  create, rewritten `claimed_at + run budget` by `_claim_one`, and rewritten back to the queue's by
  `_requeue`. That matters because five separate readers already consult that one column — the
  reaper's candidate select and its guarded UPDATE, the 410 gate, the SSE stream's own exit, and both
  read surfaces — so a second deadline column would have been a second thing each of them had to
  choose between. It is also what makes the 410 rule correct with **no code change at all**: the gate
  reads whichever budget applies, so it cannot drift from what the reaper enforces. `claimed_at` is
  the new column, and it carries the fact the reason is derived from.
- **`timed_out` stays one state; the slug carries the distinction.** `queue_expired` joins
  `deadline_exceeded` and `retries_exhausted` in `error`, for the reason issue 19b gave for not
  adding a state: `TERMINAL_STATES` is a lockstep set the provider reports verbatim and
  `TASK_ACTIVE_STATES` is its complement and the predicate of `tasks_one_active_per_member`, so a new
  state costs a lockstep release and buys nothing the slug does not.
- **The retry goes back onto the queue clock**, which the issue's "unchanged" list did not
  anticipate. Leaving it alone would have left the bug intact one level down: a task whose provider
  died rejoins a queue that is hours long while still measured against that provider's run deadline,
  and is reported `deadline_exceeded` — "fix the task" — for waiting. The re-anchor is to
  `created_at`, never to now, so waiting stays bounded from when the member asked and a fleet that
  keeps dying cannot extend it one window at a time. Worst-case lifetime is therefore
  `queue budget + attempts × run budget` — seven hours at the shipped 4h/1h/3, against one hour
  before — and `grid task cancel` (19b) is what makes that affordable, which is why it landed first.
- **`task_deadline_seconds` keeps its name** and now means the run budget. Renaming it would leave an
  operator's existing `TASK_DEADLINE_SECONDS` reading nothing and silently revert them to the
  default, which is a worse failure than a name that has narrowed. The new knob is
  `TASK_QUEUE_DEADLINE_SECONDS` (4h), and the inequality between them is load-bearing rather than
  incidental: a queue budget SHORTER than a run budget reintroduces this bug in miniature.
  ⚠️ **It is enforced at BOOT, not only in a test** (`config.validate_task_budgets`, fatal, beside
  the git ≥ 2.38 check). Review found that the cross-repo assertion in autonomous-grid's
  `tests/test_task_lease.py` AST-parses the two DEFAULTS out of `config.py` — so it guards what
  grid-src ships and is structurally blind to what an operator sets, and it skips entirely in CI,
  which checks out one repository. Inverted, the pair produces a relay that boots, serves every
  request, logs nothing, and reaps each reclaimed task on the next tick with its attempt already
  spent and a reason pointing at capacity: `_requeue` anchors to `created_at`, so a queue window
  shorter than the run the task just had is already in the past when it is written. A refusal to
  boot is the only place that can be caught, because it is the only place both numbers are read
  together.

⚠️ **The measurement that changed how this is tested.** httpx's `ASGITransport` buffers a response
body before returning the object, so `client.stream(...)` **never returns** on a stream that has no
end — and a live queued task's stream having no end is exactly what D-k is for. Every existing
stream test in grid-src works because its task is terminal. So the in-process test drives
`read_task_events` as a function (the refusal is an exception; the stream is a return value it never
iterates), and the real-socket assertion lives in autonomous-grid's cross-repo E2E, which needs no
provider and no agent and is the cheapest test in that directory.

### D-l — The client is software, so a refusal is data

Every refusal in the task plane is a prose `detail` string: the create-time 409 carries no task id,
the promote refusal is asked to say "how far behind" in a sentence, the settle failures are
distinguished only by wording, and the push refusal is deliberately identical for every caller.

That was right when a person read it. This client is an application, and an application that has to
regex-match an English sentence to decide what to show is one release away from silently mis-handling
a reworded message — the drift this repository already warns about for duplicated wire constants,
reached through prose instead.

So every 4xx in this plane carries a **stable machine-readable code** alongside its sentence, added
before the application exists rather than retrofitted under one already matching on text. The
sentence stays, and stays human: the code is what the client branches on.

**As built (issue 19a).** All 120 4xx sites in the plane go through `task_errors`, and two questions
the design left open are answered here rather than inherited:

- **A code exists so a client can act differently, so shape validation shares one.** Fifty codes for
  "name must be a string" / "limit must be 1..200" would be fifty chances for a client to disagree
  with the server about which applies, and a client's handling of all of them is identical. Those
  answer `invalid_request` and name the offending `field`; a refusal a client acts on
  (`promote_not_fast_forward`, `member_has_active_task`, `project_has_no_trunk`) keeps its own code
  and its own fields. `body_limit`'s 413 is the one carve-out — that middleware fronts the inference
  plane too, so recoding it changes a contract for callers with nothing to do with this feature.
- **The project-shaped 404 is coded, and its code must not discriminate.** `no_such_project` is
  answered identically for "no such project" and "you are not in it", because the id is the only
  thing between one team's source and another's. That is safe only while the code is the *same* one
  in both cases, which is why the sentence and the code are now produced together, once
  (`task_errors.no_such_project`) instead of copied into five modules by hand.
- **The read surface.** `GET /tasks` (nothing listed tasks at all), `GET /projects/{id}/status`
  (how far behind, and what holds the caller's slot — a promote attempt was the only way to ask),
  and `GET /projects/{id}/integrate/preview` (the dry-run: integration *was* the conflict check, and
  asking cost a task slot and could queue a paid agent run). The preview shares ONE tier decision
  with the integration it predicts (`project_integrate.decide_tier`), because a preview that
  disagrees is worse than no preview.
- **A project member may read another member's task.** The Consequences below call `owner_id` "the
  wrong default for a shared project", and this takes that decision: `GET /tasks/{id}` and the event
  stream fence on `project_members`. Nothing new is disclosed — a member can already clone the
  project and fetch any `task/<id>` branch, and the committed `.grid/agent/<key>/` transcript is
  shared by construction. The 404/403 split is kept: a task id is an opaque uuid, and collapsing the
  pair makes "did my create land?" unanswerable.
- **The status read is the change signal**, rather than a new activity table. `main_commit` moves on
  a promote or an import and each member's tip moves on a settle, an integration or a commit, so an
  application diffs an oid it already holds. A durable activity log was rejected because tiers 1 and
  2 of an integration and `project commit` all DELETE their task row, so there is nothing in the
  database to build one from without adding writes at five more sites.

**Deferred to issue 19b, with the mechanism already chosen rather than left open**: cancelling a task
(the relay writes a terminal state and the provider's next lease renewal gets the 403 its renewer
already kills on), and making a provider's capacity withdrawal member-visible (a `tasks_paused_until`
key in the heartbeat's `load`, following `unhealthy_models` — absent ⇒ nothing withheld, so the
rollout is free in both directions). `/status`'s `queue` block ships the half of "why is nothing
moving" the relay can answer alone.

**As built (issue 19b).** Both shipped, and the first of them not by the mechanism named above.

- ⚠️ **"The next lease renewal gets the 403" is wrong, and it was measured rather than reasoned
  about.** A cancelled row keeps its `provider_id`, so `tasks._refuse_unleased` falls *past* the
  "another provider holds this" branch — that one requires `provider_id != caller` — and answers
  **404 `task_not_running`**, which is exactly the answer the renewer deliberately does NOT kill on,
  because a relay too old to have the lease route sends an indistinguishable one. Left as written,
  cancel would free the member's slot at once and the agent would go on spending the operator's
  subscription until it finished by itself. So the kill signal is the refusal **code**
  (`task_cancelled`) on that 404, which makes the renewer the one place a provider reads a parsed
  `detail`. The ambiguity the 404 branch protects is untouched — no code means no verdict — so
  *absent ⇒ the pre-19b behaviour* and there is **no rollout order**.
  The relay-side branch is gated on **terminal state AND the error slug**, never the slug alone:
  `error` also carries whatever a provider reported, and a reclaim leaves the previous attempt's on a
  requeued row, so the slug by itself would kill a healthy agent.
- **A terminal `failed` with a reason, not a fourth state.** `TERMINAL_STATES` is a lockstep set the
  provider reports verbatim and `TASK_ACTIVE_STATES` is its complement and the predicate of
  `tasks_one_active_per_member`, so a new state costs a lockstep release and buys nothing the slug
  does not. Writing `failed` is also what frees the slot. `preparing` is refused with its own code: it
  is the in-flight half of a create, an integrate or a commit, and ending it strands a git operation.
- **Membership is the fence**, the same one `GET /tasks/{id}` uses. On a shared project the colleague
  whose merge task has been stuck for an hour is precisely who needs to stop it, and cancel discloses
  nothing a read does not. The event log records who did it, by `member_key`.
- **Nothing is rewound.** No ref is touched, so `grid task fetch` still works on a cancelled task —
  and the provider is fenced anyway, because `task_git._access` grants push on `state == 'running'`.
- **Capacity is published, and only published.** `tasks_paused_until` reaches `/status` as
  `providers: {online, paused, resumes_at}`, fleet-wide because the task queue is. Nothing routes on
  it and nothing consults it before handing out work — the claim path is untouched, and a paused
  provider still simply does not ask. `resumes_at` is the EARLIEST of the paused nodes, because when
  the fleet next regains capacity is the number a team decides on.

### D-n — The provider confines the agent, and hands it the confinement per invocation

A task is arbitrary code execution as the provider's user, by design — the agent has to run builds,
tests and installs, and that is the product. D-f closes the vector that fires *before* the model
decides anything; it does not change that. So the question is not how to make a task safe, but what
the provider is willing to lose and how the blast radius is bounded.

Claude Code has a first-class sandbox for the commands the model runs — bubblewrap on Linux, seatbelt
on macOS — and it fits this design. **Measured on 2.1.223 / macOS 26.6, with the provider's own argv
shape**, reading a file outside the workspace:

| Configuration | Outcome |
|---|---|
| today's argv (`bypassPermissions`, no sandbox) | read succeeded |
| `sandbox.enabled` + `autoAllowBashIfSandboxed`, no deny rules | read succeeded — the default confines **writes**, not reads |
| deny rule written `Read(/abs/path/**)` | read succeeded — the path is treated as project-relative and silently prefixed |
| deny rule written `Read(//abs/path/**)` | **blocked at the kernel**: `Operation not permitted (os error 1)` |
| the same, with a repository `.claude/settings.json` setting `sandbox.enabled: false` | **still blocked** |

Four things follow, and the second is the one that will bite.

**It is enforced by the OS, not by the model.** The refusal is an `EPERM` from the kernel, and the
agent reported that it could not override it — `allowUnsandboxedCommands: false` holds.

**The path syntax fails open.** A single-slash absolute path in a permission rule is read as
project-relative, produces a path that matches nothing, and the read *succeeds*. A provider that gets
one slash wrong has deployed a control that does nothing and looks configured. Any deny list this
feature relies on needs a test that proves the denial, not a test that proves the setting is present.

**Reads are not confined by default.** `sandbox.enabled` alone leaves `$HOME`, `~/.ssh` and the
config directory readable. The deny list is the control; the switch is only what turns the machinery
on.

**`--settings` outranks the repository's settings**, so the policy travels with the invocation rather
than depending on a file the provider hopes is right. That is complementary to D-f's
`--setting-sources user`, not a replacement: `--settings` *delivers* the provider's policy,
`--setting-sources user` *stops* the repository's hooks and MCP servers from loading at all.

`autoAllowBashIfSandboxed` is what makes this usable unattended — it approves Bash *because* the
command is confined, instead of disabling the permission system wholesale. That is the third option
the provider has been missing, and it is worth noting that Claude Code's own help recommends
permission-bypass *"only for sandboxes with no internet access"* — the opposite of the provider today.

**What this does not solve.** The sandbox confines the commands the model runs; it does not confine
the Claude Code process, which holds the provider's subscription credential and must be able to read
it. A malicious task can still take that credential, and no configuration prevents it — only running
the provider as a dedicated unprivileged user, or in a container, bounds what else goes with it.
Combined with `_require_provider` checking only the grid role, the standing statement is: **any grid
member can execute code on any provider.** For an internally operated fleet that is a decision; it
must be a stated one, not one inherited from ADR 0032, which was written when every repository was
one the relay had grown from empty.

The measurement was made on macOS/seatbelt. Providers run Linux/bubblewrap, a different backend that
must be installed — so this is re-measured on the fleet before it is relied on.

> **Re-measured on Linux, 2026-08-06 (issue 23)**, on a dev VM (Ubuntu 24.04, kernel 6.8, Claude Code
> 2.1.223), isolated in a temp directory. Four things the macOS run could not have shown, and the
> second one would have been a fleet-wide outage:
>
> - **The dependency is `bubblewrap` AND `socat`.** Every note in this feature said bubblewrap alone;
>   with only that, the run still refuses. A stock provider VM has neither, and no Claude Code.
> - **Ubuntu 24.04 breaks the sandbox by default.** It ships
>   `kernel.apparmor_restrict_unprivileged_userns=1`, and the sandbox nests a user namespace per
>   command, so *every Bash call in every task* failed — while the read denial still passed, which is
>   precisely the shape that reads as success. Reproduced beneath Claude Code: one `bwrap` works,
>   `bwrap` inside `bwrap` does not. Root does not help; the capability is dropped by the outer
>   namespace. Fixed with `sandbox.enableWeakerNestedSandbox`, set on Linux only — undocumented and
>   named "weaker", so measured rather than trusted: with it on, a denied path still comes back
>   absent from `cat` and `pip install` works.
> - **`failIfUnavailable` is genuinely fail-closed on Linux** — exit 1 naming the missing packages,
>   before any model call. That is what made shipping ahead of this measurement safe.
> - **The macOS certificate-trust failure does not reproduce on Linux**, so the egress allowlist is
>   viable on the fleet. On macOS a task cannot install dependencies (`trustd` is blocked inside the
>   sandbox); that stays a documented development-box limitation rather than a fleet one.
>
> The decision is unchanged; what changed is what has to be installed, and one setting that Linux
> cannot work without.

### D-m — A commit says who asked for it, and the grid says it made it

Both `_env()` implementations — the relay's and the provider's — force the same four variables:
`GIT_AUTHOR_NAME` and `GIT_COMMITTER_NAME` to `grid`, both emails to `grid@invalid`. Every commit in
a project therefore has the same author, for every member, forever.

That was right in ADR 0032, and for a reason rather than by omission: `commit-tree` refuses to run
without an identity, and with one person per project there was nothing to distinguish. It becomes
wrong at the moment this ADR creates — several people, and an imported repository with a real history
of real authors to append to. After a few weeks `git blame` answers `grid` for every new line, and
which member's task produced a change is unanswerable from the repository, while the relay has held
`owner_id` on the task row the whole time.

So the two identities separate, which is what git's own model is for:

- **author** — the project member whose task this is, resolved from `UserRow` (`email` is non-null
  and unique; `name` is available too);
- **committer** — `grid`, unchanged.

That is exactly git's semantics: this person asked for the change, this system produced it. It also
keeps the property the design leans on elsewhere — a commit object is still evidently machine-made,
and `blame` still attributes the intent.

The provider builds the *result* commit, so the author identity travels on the claim payload beside
`member_key`. *Absent ⇒ fall back to `grid <grid@invalid>`*, which is today's behaviour exactly, so
an older provider degrades to anonymity rather than to a failure.

The email is deliberately allowed here where D-c refused it in a ref name. The cases are not the
same: a ref name is a permanent structural feature of every clone, whereas a commit author is the one
field in git whose entire purpose is to carry a person's address, and stripping it is what causes the
problem this decision exists to fix.

## Consequences

- **ADR 0032 D-e is split.** Its first half survives unchanged: the provider commits at terminal
  boundaries and pushes its task branch on success and failure alike. Its second half — `main` as
  the base of the next task — is replaced by D-c. `DEFAULT_BRANCH` is currently one constant serving
  both meanings, in both repositories; it becomes two.
- **The one-active-task index changes key**, from `project_id` to `(project_id, owner_id)`, and
  three other sites that named the old invariant change with it (see the table above). The index is
  now load-bearing for two rules rather than one: it limits a project member to one task, *and* it
  is what keeps integration from racing that task (D-d).
- **The claim payload carries the task's author.** Nothing in it identifies the member today, and
  D-g's workspace path needs one. Deriving it by parsing `wip/<member_key>` out of the base ref instead
  would duplicate the `wip/` literal into the provider — the duplication the base ref exists to
  avoid — and would degrade wrongly: a missing field would change the workspace path, therefore the
  transcript directory name, therefore make the conversation permanently unresumable. That is a new
  silent failure, not a fallback.
- **`_capped_body` is the import path's real ceiling, not the validator.** It buffers the whole
  request body in memory and caps it at 64 MiB (`task_git_max_bytes`), and it applies to
  `git-receive-pack`, so the staging push hits it too. The import path streams to a temporary file
  under its own, larger limit; the ordinary push path keeps the 64 MiB in-memory bound, because a
  task's result push has no reason to need more. A streaming import route must not grow a second
  copy of the pkt-line parser or the ref check — `tasks.py` states the rule about that: *"three
  copies of a security decision drift, and the drift is silent"*.
- **The response side of the git plane is unbounded, and import is what finds it.** `task_repo.rpc`
  runs `upload-pack` through `subprocess.run(capture_output=True)` and `_serve_rpc` answers with
  `Response(content=data)`, so a provider's first fetch of a project holding real history buffers
  the entire packfile in the relay's memory, with no ceiling at all. The same call is bounded by
  `_GIT_TIMEOUT_SECONDS`, which is **60 seconds**. And a fetch failure inside `materialize` produces
  a **terminal** `failed` outcome, so an imported history too large to pack in 60s does not degrade —
  every task in that project fails immediately and is never retried. The transport is fixed before
  import is offered.
- **`git clean -ffdx` runs before every task and `-x` removes ignored files**, so a real project's
  `node_modules/`, `.venv/` and build caches are deleted and rebuilt for every task.
- **Promotion is open to every project member, and fast-forward-only means one of them cannot
  destroy anything** — but unreviewed code can still reach `main`, and `receive.denyNonFastForwards`
  blocks undoing it by push. A revert is therefore the relay's own `update-ref`, the mechanism
  `reset_branch` already uses for reclaim. The project-role column exists to narrow this later
  without a migration; nothing consults it yet.
- **A project member cannot read another member's task, and nothing lists tasks.** `get_task` and
  `read_task_events` fence on `owner_id`, and there is no list endpoint. Under one slot per member
  the create-time 409 becomes a routine answer — and its message names the project, which is now the
  wrong noun; it has to name the member's own active task. A read path that says which task holds a
  member's slot is needed for the 409 to be actionable.
- **There is no project-scoped API at all, and an application needs one.** The whole route list is
  seven task routes and three git routes. Missing: create a project, list my projects, list a
  project's tasks, see who holds a member's slot, see how far behind a WIP branch is, **check for
  conflicts without integrating** (integration *is* the check today, and it spends the slot and may
  spawn a paid agent run), cancel, and any signal that the project changed. The only change
  notification available is polling `git fetch` — against the transport that is already unbounded and
  60-second-capped. This is a slice of its own, not a footnote on the others.
- **Nothing prunes, and what grows fastest is what every clone downloads.** `delete_branch` has one
  caller, a failed prepare, so `task/<uuid>` refs accumulate for the project's life. A *failed*
  task's branch is merged nowhere by design and holds a full transcript blob: seven members at five
  tasks a day with one in five failing is seven dead branches a day, which at ADR 0032's measured
  105 KB–2 MB per transcript is 0.2–3.5 GB a year, all inside the pack a new member's first clone
  pulls. A retention rule for task branches, and a member-side `hideRefs` policy — members currently
  get `hide_refs = ()` and are advertised every ref — are decided before import ships, not after.
- **Task capacity is process-wide and member-blind.** One member's task producing a rate-limit
  reading withdraws that provider from the whole team for the vendor window, and the explanation
  reaches only the client whose task was running — the other six see a queued task and then D-k's
  silent reaping, in a stream they are fenced out of. The argument for publishing nothing ("the relay
  needs to know nothing") was written for one member per project and does not survive six other
  people waiting on the same subscription.
- **The git RPC's memory bound is threadpool × packfile, not one packfile.** `advertise_refs` and
  `rpc` run under `asyncio.to_thread` and each buffers a whole pack, so seven polling clients plus
  the provider fleet multiply the ceiling by the thread limit. An explicit concurrency limit on the
  RPC path belongs with the streaming fix, not after it.
- **The slot rule quietly rewards one project per person.** The index is `(project_id, owner_id)`, so
  a member in seven projects has seven slots and never blocks on integration. Given the cost of a
  conflicting integrate, the rational team response is for everyone to take their own project — at
  which point there is no shared `main`, no integration, no merge task, and this ADR reduces to ADR
  0032 with new nouns. The intended unit is **one project per shared codebase**, and that is stated
  here because nothing in the mechanism enforces or measures it.
- **No migration is required.** The `tasks` table exists only on `feat/distributed-tasks`;
  `origin/main` never had it, so no live grid holds a project, a workspace or a transcript at the
  old paths. That window closes when this feature ships.
- New vocabulary — *project member*, *project role*, *WIP branch*, *promote*, *integrate*,
  *import* — is settled before implementation, as this repository requires, and each of the first
  three exists to avoid colliding with a word the glossary had already spent.
- Deferred with the shape kept open: project roles that actually gate promotion, protected-branch
  rules beyond fast-forward-only, LFS, per-member task capacity, and tags inside a task's checkout.
