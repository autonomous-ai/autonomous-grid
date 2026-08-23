---
status: proposed
---

# A task is a conversation, and nobody merges by hand

[ADR 0033](./0033-a-project-has-its-own-members-so-main-stops-being-the-base.md) built the task plane
for a five-to-seven person team **of developers**. Three of its decisions were written for that
reader and only for that reader:

- a task is one prompt, and a project member's tasks are turns of a single conversation belonging to
  the `(project, member)` pair (D-g), so **one task at a time per member** is not a limit but the
  shape of the thing;
- `main` is a release branch, and **a person decides** when work lands on it — `promote` (D-b);
- when `main` has moved underneath somebody, **a person decides** to bring it back — `integrate`
  (D-d, D-e).

The product now serves people who do not read diffs. They reach the grid through a desktop
application that spawns this CLI. Four changes are wanted:

- **many conversations at once in one project**, each its own Claude Code session, with follow-up
  messages inside one conversation processed in order;
- **collaboration is kept** — several people in one project, as 0033 built;
- **anyone on the grid can work in any project of it**, without being invited to each one;
- **no `promote`, no `integrate`, no merge** — because there is nobody to do them.

The fourth is the one that forces the rest, for the same reason the release branch forced 0033.
0033's D-b removed `main`'s second job — being the base — and gave it to `wip/<member_key>`, on the
grounds that the first job needed a human decision. **Take the human away and `main`'s first job has
no owner either.** Everything below follows from deciding that the relay owns it.

> **Revision note (2026-08-13).** The first draft of this ADR was reviewed against the code by five
> independent passes. They found four factual errors and nine holes that would each have shipped a
> bug with a green suite. Every correction is folded in below and marked ⚠️ **CORRECTED** where the
> first draft said something false, so a reader who saw the earlier version can find what changed.
> The lesson that produced them: the first draft's decisions were reasoned from the code, and its
> *slices* were then written from the ADR rather than from the code.

## Vocabulary before anything else

This repository enforces its vocabulary, and this ADR re-levels the two words that carry the most
weight. Read this section before the decisions or half of them will read as contradictions.

| Word | Before this ADR | After |
|---|---|---|
| **task** | one prompt, one agent run, one `TaskRow` | **a conversation**: one Claude Code session, many turns, one long-lived thing a person names and returns to |
| **turn** | did not exist | **one unit of work**: one user message, one agent run, one provider lease — exactly what `TaskRow` is today |

`TaskRow` therefore becomes the **turn** row and a new row carries the **task**. The word *thread* is
used nowhere. `promote` leaves the vocabulary entirely; `integrate` survives only as the name of the
relay's internal tier decision, never as something a person does.

⚠️ **`task_id` is already a wire key and already means the turn.** It is what `POST /tasks/{id}/lease`,
`/result`, `/events` and `/cancel` address, and what the provider hands to its lease renewer. The
conversation's id is therefore a **new key with a new spelling, `conversation_id`**, and no route
changes which object its path parameter addresses. A design that let one spelling mean both is how a
provider renews the wrong id, gets a bare 404, stops renewing, and leaves the agent running until the
task is reclaimed — with nothing red anywhere.

## What breaks first, and it is not the index

Relaxing "one active task per project member" to "one running turn per conversation" looks like the
same one-line index change 0033's D-c already made once. It is not, and what it takes away is not the
limit.

**`tasks_one_active_per_member` is doing three jobs and is named for one.**

1. **Uniqueness** — the job it is named for.
2. **Fairness.** `_claim_one` orders the queue with `.order_by(TaskRow.created_at)` — global FIFO,
   across every member and every project on the grid. That is safe today *only* because the index
   guarantees each person has at most one row in that queue. Re-key it and a member who opens twenty
   conversations puts twenty turns in front of everyone else's next one. Nobody did anything wrong;
   it is the default, and the symptom is "the grid is slow", which names nobody.
3. **Serializing input preparation.** `task_prepare`'s module docstring states it: *"Everything here
   exists to keep ONE invariant true: `preparing` is inside the one-active-task index predicate
   (`TASK_ACTIVE_STATES`), so a row parked there holds its member's slot against every later
   create."* `TASK_ACTIVE_STATES` is `("preparing", "queued", "running")`. And `commit_input` writes
   the branch ref with a **bare, non-compare-and-swap `update-ref`**, safe only because — in its own
   words — *"the ref it names is one it has just minted, so there is nothing to race with."*

Narrowing the predicate to `state = 'running'` removes job 3 silently. Two messages typed half a
second apart — the exact behaviour this ADR exists to support — then both reach `_prepare_input` on
the same base ref, and the second `update-ref` overwrites the first turn's input commit. The first
turn still runs, because the object is reachable from the second commit; its uploaded files are gone;
and **which turn ran first is decided by whichever git call finished, not by `created_at`**.

This is the third time this table has been found doing an unwritten second job — 0033 opened with the
same discovery about `tasks_one_active_per_project`. The rule to carry forward is not about either
index: **before removing a uniqueness constraint, enumerate what reads the queue it was keeping
short, and grep the constraint's NAME across every repository.**

## What the re-level touches beyond the index

Each site below was read before being listed. Several are in the other repository.

| Site | What it assumes | What this ADR does to it |
|---|---|---|
| `db.py:628` — `tasks_one_active_per_member` | one active row per `(project, member)` | re-keyed to one **running** row per conversation, **plus a second index keeping `preparing` unique per conversation** — D-b |
| `tasks.py:547` — the claim's `ORDER BY` | the queue holds at most one row per member | the cap moves into the SELECT **and into the popping UPDATE's WHERE** — D-i |
| `tasks.py:598` — `resume_session_id` | the session belongs to `(project, owner)`; found by "the most recent task that has one" | becomes a column on the conversation row, and that fragile query is deleted — D-c |
| `task_prepare.py` — `hold_member_slot`, `active_task_for`, `expire_abandoned_prepares` | the index covers `preparing`; at most one row per member; abandonment is scoped per project | all three re-key to the conversation — D-b |
| `project_status.py` — `_active_task`, `_slot_holders` | `.first()` with no `ORDER BY`, and a dict collapse, **both under docstrings citing the index by name as permission** | both must order and return sets — D-b |
| `remote/tasks.py:937` — `_reserve_workspace` | a workspace belongs to `(project_id, member_key)` | re-keyed to include the conversation — D-c. **Two further constructions of the same path exist in that file** |
| `remote/task_repo.py:410` — `materialize` | resets the workspace to `input_commit`, which only the member's own work moved | now resets to a base other people moved — D-f |
| `task_git.py:197` — `push_refs` | a provider writes its leased turn branch and nothing else | gains exactly one more ref, per lease — D-j |
| `task_git.py:301` — `_hide_refs`'s **member arm** | returns early; anything outside `refs/heads/`+`refs/tags/` is invisible to a member | must un-hide the new namespace **in both arms** — D-j |
| `task_reaper.py:254` — the queue deadline | a queued turn is waiting for a **provider** | a queued turn may be waiting for **its own conversation** — D-h |
| `task_reaper.py` — `prune_dead_branches` | a branch dies when its row goes terminal | a conversation has no terminal state; its branch needs its own rule — D-e |

## Decision

> **A task is a conversation with its own session, its own workspace and its own branch; a turn is
> the unit of work, and only one turn of a task runs at a time. Everything a turn needs to run — its
> base, its input commit, its delta and its queue clock — is computed at ONE moment, when the turn
> becomes eligible. Every successful turn is applied to `main` by the relay, without being asked,
> outside the request that reported it. `promote` and `integrate` stop being things a person does.**

Fifteen decisions follow.

### D-a — A task is a conversation; a turn is the unit of work

A new `tasks` row (the conversation) owns `conversation_id`, project, owner, name,
`claude_session_id` and `created_at`. Today's `TaskRow` becomes `turns`, keyed by `conversation_id`,
and keeps everything else it has.

`TERMINAL_STATES` and `TASK_ACTIVE_STATES` stay on the **turn**, unchanged and unrenamed on the wire,
because a state is a property of a run and always was. A **third copy** lives in
`task_events._TERMINAL_STATES` and a fourth in the CLI; all four stay in step.

A conversation has no state machine of its own. "Is this conversation finished" is not a fact the
grid owns.

⚠️ **The rename is a table rename plus a data move, and no machinery in the relay can do either.**
`create_all` creates tables and never ALTERs, renames or drops; `_ensure_columns` is
`ALTER TABLE … ADD COLUMN` only; `_drop_superseded_indexes` drops named indexes. On the live grid a
table already called `tasks` exists in **turn** shape: `create_all` would leave it, add conversation
columns to it, and create `turns` empty. The master boots, then every claim reads a column that is
not there. This needs a hand-written, idempotent rename-and-backfill step beside
`_drop_superseded_indexes`, and a test that runs it against a database built the old way.

### D-b — One running turn per conversation; queued turns are allowed and ordered

`UNIQUE (conversation_id) WHERE state = 'running'`, replacing `(project_id, owner_id)`.

That line does both halves of the requirement: **sequential inside a conversation**, because two
turns of one conversation can never run at once; **concurrent across conversations**, because nothing
keys on the member any more.

Typing ahead is supported rather than refused, exactly as Claude Code accepts a queued message, and
the turns run in `created_at` order.

⚠️ **A second index is required and is not optional:** `UNIQUE (conversation_id) WHERE state =
'preparing'`. Without it the narrowing removes the only thing serializing `commit_input`'s
non-compare-and-swap ref write, and two typed-ahead messages race on the conversation's base ref (see
*What breaks first*). `expire_abandoned_prepares` re-keys from the project to the conversation in the
same slice, or a live prepare in one conversation can be failed by an abandoned one in another.

The claim must skip a turn whose conversation already has one running: a `NOT EXISTS` **inside**
`_claim_one`'s SELECT, never a filter applied to its result — the candidate SELECT is capped, so a
filter afterwards starves a conversation whose eligible turn sits past the cap.

⚠️ Everything concurrency needs must land in the same release as this line: per-conversation
workspace and session (D-c), per-conversation base (D-e), the provider's own reservation (D-c, in the
other repository), the fairness the old index was silently providing (D-i), and the `preparing` index
above.

### D-c — A workspace, and therefore a session, belongs to a conversation

`/var/grid/projects/<project_id>/<member_key>/<conversation_id>/workspace`.

0033 D-g established the mechanism and this extends it by one level: Claude Code derives a session's
transcript directory from its **resolved absolute working directory**, so one session per
conversation requires one working directory per conversation. `member_key` stays in the path — it
keeps the containment rules written in terms of it, and keeps the isolation boundary visible.

`resume_session_id` becomes a column on the conversation row; the "most recent turn of this
`(project, owner)` that has one" query is deleted rather than re-keyed. Settle writes
`claude_session_id` to the **conversation**, never to both rows.

⚠️ **`conversation_id` on the claim payload is NOT a safe degrade** — exactly `member_key`'s class
(0033 D-g): absent ⇒ a different workspace path ⇒ a different transcript directory ⇒ that
conversation is permanently unresumable, while the turn completes and every signal reads healthy. The
provider **refuses** the claim, terminally, with a message naming the relay and **distinct from** the
existing missing-`member_key` refusal — a fused message tells an operator to upgrade for the wrong
reason. **Roll the relay out BEFORE the provider fleet.**

⚠️ **The workspace path is built in three places in the provider** — the reservation, the live-tree
beat, and the result push. Re-keying one and not the others snapshots or pushes from the wrong
worktree, and the tree beat degrades to `None` on any fault, so it fails silently.

⚠️ **Per-conversation workspaces need a layout and a bound.** Three conversations of one member as
separate clones cost **3.28 GB** — MEASURED (issue 35, below), on the same repository 0033 issue 16a
used, which has since grown to 792 MiB / 34,159 commits. One local clone per `(project, member)`
with a `git worktree` per conversation shares the object store and is **viable**: measured, the Nth
worktree adds **305.8 MiB** against the Nth clone's **1,043.4 MiB**, so three conversations cost
1.62 GB instead of 3.28 GB. **Workspace eviction is still required** — `GRID_MAX_TASKS` bounds
concurrent runs, not accumulated directories.

> ⚠️ **AMENDED 2026-08-14 — this paragraph asserted "~12 GB of checkouts" and that figure is wrong
> by ~3.8×.** Nobody had taken it; it was the estimate issue 35 existed to replace, and the
> measurement contradicts it. Amended rather than reinterpreted, per issue 35's eighth criterion.
>
> **The conclusion survives and its justification is weaker than this ADR implied.** Sharing the
> object store really does pay — 52.9% of the equivalent clones, and 3.4× cheaper for each
> conversation after the first — so D-c's layout stands on the measurement. But it stands on 1.6 GB
> saved out of 3.3 GB, not on 12 GB, and any future argument that reaches for the old figure is
> reaching for a number that was never measured. `tests/measure_non_dev_design.py --tier git`
> re-takes it.

### D-d — The relay applies every successful turn to `main`, and it does so OUTSIDE the settle request

A successful turn is applied to `main`: fast-forward when it can, a merge commit when `merge-tree` is
clean, a merge turn when it is not (D-g). No member action, no button, no diff to approve.

⚠️ **CORRECTED — the apply does not run inside `POST /tasks/{id}/result`.** The first draft put it
there, under a per-project lock. The provider stops renewing its lease *before* it sends that report,
deliberately (*"Renewal stops FIRST, and before the terminal report below"*), so everything the relay
does inside that request must fit in what remains of the lease TTL. Measured from the code: 120 s TTL
plus a 5 s reaper tick, minus a 65 s worst-case renewal gap and a 30 s event flush, leaves
**25–30 seconds** — today enormous headroom for one `update-ref`, and an unbounded, lock-serialized
`merge-tree` if the apply goes there. Overrun means the reaper reclaims a completed run, and — worse
— the relay may have **already moved `main`** before its guarded UPDATE fails, which is the
non-atomicity `task_result` already documents for the WIP branch with `wip reset` as the recovery.

So: **settle records the result and returns.** A separate relay step applies it. That step is
observable, retryable, and can take as long as it needs. It also gives the apply somewhere to *wait*,
which D-p below depends on.

`main`'s writers become **apply, import and init**. A member writes it through nothing.

⚠️ **A conversation of five turns puts four intermediate states into the shared trunk.** Under 0033 a
team saw each other's work when somebody thought it was ready; they now see it at every turn. That is
the deliberate trade for removing the human, and D-l is what makes it survivable.

### D-e — A conversation has a branch, and it is the re-keyed WIP branch

⚠️ **CORRECTED — `wip/` is re-keyed, not deleted.** The first draft deleted `wip/<member_key>` and
rebuilt an equivalent under a new name. That orphaned `project_commit` (which writes onto a member's
WIP branch with no agent — 0033 D-j), `project_status`'s rendering, the fence's `author_keys`
advertisement, and the retention thinking around all of them, none of which the first draft's slices
mentioned.

**`wip/<conversation_id>`** is the conversation's branch. A turn is cut from it, pushes to
`task/<turn_id>` exactly as today, and settle fast-forwards the conversation branch — **on failure
too**, which is what makes the next turn's files match what the conversation remembers.

The turn's branch name, the fence's grant of it, and `grid task fetch` are **unchanged**.

⚠️ **Retention needs a second rule.** `prune_dead_branches` deletes `TaskRow.branch` for terminal rows
past the window — correct for turn branches. A conversation has no terminal state, so the same sweep
keyed the same way would delete a live conversation's branch as soon as one of its turns aged out.
Conversation branches and transcript refs are collected on project deletion and on a conversation-level
idle rule, never on turn terminality.

### D-f — Everything a turn needs is computed when the turn becomes ELIGIBLE

⚠️ **CORRECTED, and this is the largest change from the first draft.** A turn becomes **eligible**
when its conversation has no running turn. At that moment, and nowhere else, the relay:

1. refreshes `wip/<conversation_id>` from `main`;
2. computes the turn's `input_commit` against the refreshed branch;
3. computes the delta block;
4. anchors `deadline_at` (D-h).

The first draft had these in three different places and left two of them unstated. Putting them at
one moment fixes four separate defects:

- **`input_commit` was written once, at create.** Three messages typed at 10:00 all got the same
  base; turn 2 would hard-reset the workspace to a commit predating turn 1, so **the turns would not
  compose** and turn 2's result would revert turn 1's. Eligibility is the first moment the correct
  base is known.
- **The delta's baseline was wrong.** The first draft diffed the previous turn's *input* commit
  against this one — and between those two sits the previous turn's own **result**, because D-d
  applies it. The block would have told a session that its own work was somebody else's change,
  which is the failure D-f exists to prevent, inverted. The baseline is the previous turn's
  **`result_commit`**, falling back to its input commit when it produced none.
- **The refresh had no defined behaviour on a diverged branch.** After a failed turn the conversation
  branch has commits `main` does not, so "fast-forward from `main`" is not available. The refresh
  uses `decide_tier` — **ours = the conversation branch, theirs = `main`**, the opposite direction
  from 0033's integrate, stated because "reuse it unchanged" is wrong. Tier 3 queues a merge turn
  (D-g) ahead of the turn that triggered the refresh.
- **The claim had to stay free of git work.** `_claim_one` does none today, and the lease is stamped
  at the top of it. Two 60-second-ceiling plumbing calls between that stamp and the provider's first
  renewal would put the first renewal at 150 s against a 120 s TTL — reclaimed before it ever renews.
  Eligibility is not the claim; no provider is waiting on it.

The delta is a name-status list plus a stat, **not** a full diff, bounded by the same
`utf8_length` rule `create_task` applies to a prompt, and **composed into the outgoing prompt rather
than written to the `prompt` column** — a retry must not double-prepend, and the stored prompt must
stay what the person typed. It carries **no new wire value**. A delta that cannot be computed says so
distinctly from "nothing changed"; the two must not collapse into one answer.

### D-g — A conflicting apply is resolved by the conversation that caused it

`merge-tree` conflicts ⇒ a **merge turn at the head of that conversation's queue**, in the same
Claude Code session, carrying `merge_ref` and the pinned `merge_commit` exactly as 0033 D-e
specifies, marked so an application renders it as a step, and **exempt from the per-member cap**.

The agent that wrote one side of a conflict knows why it wrote it. That is the context 0033 issue 15
did not have, and the failure it measured without it — a modify/delete conflict leaving no markers,
so an agent with no knowledge of intent produced a structurally perfect merge commit with somebody's
deletion silently discarded — is why this is not a system-owned resolver.

⚠️ **"At the head" is not free.** The claim orders by `created_at`, so a merge turn created later
sorts *last*. A merge turn needs an explicit priority, or one conflict becomes one merge turn per
already-typed follow-up.

⚠️ **The cost of putting it in the user's session is real and is accepted here rather than
discovered:** the merge prompt and the whole resolution land in the transcript the person later
reads, and consume that session's context permanently. The alternative — a fresh session — loses the
intent that is the entire justification. Accepted, with the marker so the application can render it
as machinery rather than as words the person wrote.

⚠️ **A merge turn that itself fails leaves the conversation unable to reach `main`.** Retrying it
re-runs the same conflict, and `max_attempts` then ends it with a message naming file paths. The
apply step must be able to hold the result — see D-p — rather than leaving the person at a terminal
failure they cannot act on.

### D-h — The queue clock starts when a turn becomes eligible

`deadline_at` is anchored at eligibility (D-f), not at creation.

`task_reaper` anchors it at `created_at + task_queue_deadline_seconds` and kills what passes as
`queue_expired` — a lockstep slug the CLI branches on to say *no provider ever took this, add
providers*. A follow-up typed ahead is not waiting for a provider; it is waiting for its own
conversation. Left as is, a person who types four messages is eventually told to buy hardware while
the fleet is idle.

⚠️ **`_requeue` re-anchors to `created_at` on every reclaim**, under a docstring that insists on it
(*"waiting is bounded from when the member asked"*). For a turn that became eligible hours after it
was typed, that anchor is already in the past and the next sweep kills it as `queue_expired` — for a
turn a provider had just picked up. `_requeue` must re-anchor from eligibility for such a turn.

⚠️ **Eligibility means "claimable right now", and D-i's cap is part of it.** A turn held back only by
its owner's own cap is therefore **not** eligible and its clock has not started. Reading it the other
way — it is waiting for capacity, its own — is defensible and is wrong in practice: the clock would
run and the turn would die as `queue_expired`, whose message tells the person to add providers while
the fleet is idle. This reading costs no new slug and makes the queue budget measure waiting for the
**fleet**, which is what its name says.

⚠️ This is deliberately **not** a fourth task state: `TERMINAL_STATES`/`TASK_ACTIVE_STATES` are
lockstep on both sides of the wire and this repository priced and refused a fourth state once, for
cancel. Eligibility is a computable predicate, and `deadline_at` re-anchoring is a concept 0033 D-k
already defines.

### D-i — Fairness is enforced at the claim, never at create

A cap on **running turns per member**, applied inside `_claim_one`'s SELECT.

The cap is on running turns, not on conversations: a conversation is a row and a ref, and a person
may have fifty. A running turn is a provider, an agent and somebody's subscription.

⚠️ **CORRECTED — the claim is not atomic.** The first draft said *"at the claim there is nothing to
race: the predicate is evaluated inside the statement that pops the row."* It is not: `_claim_one`
does a candidate SELECT and then a **separate** `UPDATE … WHERE id = ? AND state = 'queued'`. Two
providers can each select a different queued turn of one conversation, both pass `NOT EXISTS` at
select time, and both update to `running` — violating D-b's index, raising `IntegrityError` where
nothing catches it, rolling back the whole claim transaction and answering the provider a 500.
`skip_locked` does not help (different rows) and is a no-op on SQLite, so no unit test sees it. The
predicate must be **repeated in the UPDATE's WHERE**, and the loop must catch `IntegrityError` and
continue to the next candidate.

Enforcing the cap at create is wrong for the reason the index's own comment gives, and at the claim
it buys two things a create-time refusal does not: **create never fails**, and **fairness falls out**
with no fair-share `ORDER BY` on the hottest query.

**Merge turns are exempt.** Counted, a member at the cap who hits a conflict deadlocks.

### D-j — The conversation leaves the project's history

The transcript moves from the result commit to `refs/grid/agent/<conversation_id>`, pushed by the
provider holding that conversation's lease, fast-forward only.

⚠️ **CORRECTED — `$GIT_DIR/info/exclude` does not change.** The first draft said it "collapses to one
uniform line". **It already is one, and always has been** — `_ensure_repo` writes exactly `/.grid/`,
unconditionally. The per-member narrowing lives in the **force-add pathspec**
(`git add -f -A -- .grid/agent/<member_key>`), and that is what is removed. Likewise
fast-forward-only needs no new relay code: `receive.denyNonFastForwards` is already set repo-wide.

⚠️ **`info/exclude` has no say over files git already TRACKS.** Every project that has run a task
under 0033 has `.grid/agent/**` tracked in `main`; removing the force-add leaves `git add -A` staging
every modification to them forever. The first turn per project must `git rm --cached -r .grid/agent`,
or the trunk quietly stays fat while every test on a fresh project passes.

⚠️ **The fence's member arm returns early**, un-hiding `refs/heads/`, `refs/tags/`, the member's own
import ref and the merge refs of leases they hold — and **nothing else**. A ref outside `refs/heads/`
is therefore invisible to a caller who is both project member and lease holder, which on this
product's topology is the ordinary case. That is byte-for-byte the CRITICAL measured on the dev VM
for `refs/integrate/*`, whose post-mortem sits in the source directly above that arm. **Union, never
choose**: the new namespace is un-hidden in **both** arms, narrowed to the leases actually held.

⚠️ **"The transcript is missing" gains a second cause.** Today it means the predecessor legitimately
produced none. After this it can also mean *the relay has one and the provider failed to fetch it* —
and the provider cannot tell the two apart from disk. The relay states on the claim whether a side
ref exists; a provider told "yes" that finds nothing **fails the turn** rather than starting fresh.
A failed **push** of the side ref fails the turn too: best-effort here means conversations evaporate
silently.

Two consequences beyond the requirement: it **closes** the cross-member transcript privacy hole 0033
listed as permanently out of scope, and the test asserting that leak must be deliberately flipped
rather than deleted.

### D-k — Access is a property of the grid, not of an email domain

A project is reachable by any authenticated caller on the grid unless it is private:
`projects.visibility` ∈ `{grid, private}`, defaulting to `grid`, with an `_ensure_columns` entry and
a migration-test constant.

⚠️ **CORRECTED — the argument holds for one network type, and this product targets it.** The first
draft argued that a relay serves exactly one network, so grid membership already implies the company.
The first half is true; the second follows **only on a `private-domain` grid**, where the control
plane claims one grid per email domain on a person's first login, keyed on the domain and enforced by
a unique index. There, grid *is* domain, structurally, and D-k is exactly right.

**The product targets `private-domain` (decided 2026-08-13), so that is the supported topology** —
and it makes two things operational requirements rather than defaults, in the same class as git ≥
2.38 on the relay host:

- **`GRID_PRIVATE_DOMAIN_ENABLED` must be on.** It is off by default and strictly opt-in.
- **`GRID_PRIVATE_DOMAIN_BLOCKLIST` must be non-empty** and must contain the public mail providers.
  It defaults to empty, and its own docstring warns why: unset, `k@gmail.com` provisions a grid
  shared by **every Gmail account**. Under D-k that grid then hands all of them each other's
  projects. Before this ADR the consequence was a shared *grid*; after it, it is shared *source*.

On any other network type — `permissioned-public` is what the front end creates — the roster is an
allowlist with no domain constraint, so this rule is **wider** than the change request: a contractor
reaches every non-private project, including imported company repositories. That is not the supported
topology, and `visibility` is the only instrument for it. Named here rather than discovered.

⚠️ **CORRECTED — measured 2026-08-23 (ND-21). "Grid *is* domain, structurally" is true of the DOMAIN
and false of the ROSTER, and it is false on the supported topology.** grid-apis
`store.member_for_access` checks an allowlist row **before** anything else, on every network type,
and grid-src `grid_auth._requires_allowlist` names three ways onto a `private-domain` grid — the
right domain, an allowlist row, or owning the grid — carrying the sentence *"an invited account from
another domain keeps working after the switch"*. An invited outsider is therefore **authenticated**,
so D-k gives them every non-private project on the grid, imported company repositories included,
plus a `project_members` row minted on their first read that needs a key.

**The relay does not compare domains and cannot.** `grid_auth.GridAuthContext` carries `email`;
`relay.AuthContext`, the object a route is handed, does not. The premise is enforced entirely at the
control plane, and the rule there **admits** rather than restricts.

**Decided 2026-08-23: correct the claim, do not narrow the gate.** D-k's premise reads *authenticated
on this grid ⇒ a colleague **or somebody a colleague invited***, and the blast radius above is
accepted with it. Three reasons, in order of weight: the alternative asks the control plane for a new
value saying WHY a caller was admitted (domain vs allowlist vs owner), which is a lockstep value and
a rollout order for a boundary that is already live; narrowing it would revoke access from invited
accounts that work today, which grid-apis explicitly promises will keep working; and `visibility`
already exists as the instrument for a project that must not be grid-wide. What is **not** accepted
is the justification standing while being false — that is what this correction removes.

⚖️ In fairness to the shape being accepted: the minted row is **visible** afterwards in
`grid project member list`, so an owner can see who reached the project. Nobody is notified, and the
access is granted before anyone looks.

Pinned by grid-src `test_project_visibility.TestAnInvitedOutsiderIsAColleagueToThisRule` — a
characterization test, so a future narrowing announces itself here rather than in a support ticket.
Narrowing is tracked as follow-up work, not as a defect against this decision.

⚠️ **`TASK_SERVED_DOMAINS` is untouched.** It is 0033 issue 24's *a provider serves only its own
company's domain* gate — a different axis with a different failure mode. This is the third thing in
the codebase called "domain".

⚠️ **CORRECTED — the membership row is created on the first operation that NEEDS a key, not on the
first conversation.** The first draft said "never on a read". Sixteen gates resolve a stored
`member_key` or a membership row, and several are reads: `project status` — the application's home
screen — builds a branch name from it, and so do `task list`, `GET /tasks/{id}`, the event stream,
cancel, `project commit`, `init`, `import` and the git fence. Under the first draft's rule every one
of them answered a project-shaped 404 for a project the person can see in their own project list, and
by design that 404 is indistinguishable from "the id is wrong".

Existing projects become grid-visible on deploy, **including imported company repositories**. That is
a real change of blast radius and an operator must be told before the release, not after.

### D-l — Undo is a revert of one turn's patch, written by the relay

`main` only ever moves forward. Undo reverts the **patch** a turn produced — `result_commit` against
its base — never the commit, because the same change may have landed by fast-forward or inside a
merge commit and a `revert -m 1` has to know which while a patch reversal does not.

Undo runs through the same apply step as D-d, under the same serialization. Resetting `main` to an
earlier commit is **refused**: under auto-apply there are other people's turns in between, and it is
a non-fast-forward on a ref the whole team has cloned.

⚠️ **Undo is authorized to the turn's owner and to the project owner, and to nobody else.** D-k opens
every non-private project to every grid member; without this any of them could undo any turn anywhere.

⚠️ **A conflicting revert does NOT queue a merge turn in the reverted turn's conversation.** D-g's
justification — the agent that wrote one side knows why — does not transfer to a third party's undo,
and doing it would spend another person's session and subscription on a decision they did not make.
A conflicting revert is refused with a code saying so, and the person is directed to their own
conversation.

"Already undone" is persisted state: a column on the turn, with its `_ensure_columns` entry and its
migration-test constant.

### D-m — The client is a desktop application that spawns this CLI

The application is Flutter, wrapping the CLI as a subprocess. **Desktop only** — iOS and Android
sandboxes do not permit executing a separate binary — recorded because it is invisible in the code.

What the CLI owes it: `--json` everywhere and exit codes a script can branch on; a read surface that
needs no git on the user's machine (`tree`, `blob`, per-turn `diff`, archive) over the existing HTTP
plane and the same fence; and **no git vocabulary and no raw git error** on its surface, enforced by
a named denylist rather than by review.

⚠️ **The old CLI surface is a clean break.** `project promote`, `project integrate` and their routes
are deleted. `project wip reset` survives, re-keyed to a conversation, because D-d's apply can still
leave a branch ahead of a turn's input and it is the documented recovery.

⚠️ **Deleting promote also strands what fed it**: the promotion ledger and its read route, the
integrate preview (which lives in `project_status`, not in `project_integrate`), the
`task.wip_not_advanced` event, and `member_has_active_task`, whose only two raisers both disappear.
Under auto-apply the audit question does not go away — it grows, since every turn is a release — so
the per-turn diff of D-m is what answers it, and the ledger goes.

### D-n — A conversation is started, and continued, through the same route

⚠️ **NEW — the first draft specified the queue and not the door.** `POST /relay/v1/tasks` is the only
route that creates work, and the first draft added none. A design with a per-conversation queue, an
eligibility clock, a per-conversation workspace and a resumed session had **no way to send the second
message**.

`POST /relay/v1/tasks` creates a conversation and its first turn.
`POST /relay/v1/tasks/{conversation_id}/turns` adds a turn to an existing one. Both take the same
body; the second takes no project and no files-that-bootstrap. `grid task create` and `grid task send`
are the CLI halves.

An old relay answers a bare framework 404 for the new route, which the existing missing-route hint
turns into a sentence naming the relay. **Roll the relay out before the CLI.**

### D-o — A project is usable the moment it is created

⚠️ **NEW.** Today `POST /projects` creates a repository with no commits, and the first task is
refused `project_has_no_trunk` with a message naming `grid project init` and `grid project import`.
For the reader this ADR is written for, that is the first thing they do after signing in, and it
fails with four git words and two flags.

It cannot be fixed by auto-initialising on first use: 0033 D-o forbids it, because import refuses a
project that already has a trunk, so an auto-init **permanently** closes the import path and delete
cannot recover the id either.

So the choice moves to where it can be made once, explicitly, by an application that knows what the
person said they were doing: **`POST /projects` takes the bootstrap** — `empty` (init immediately),
`import` (leave trunkless and expect a push), or nothing, which keeps today's behaviour for existing
callers. `grid project create --empty` is the CLI half, and `task create` on a trunkless project
keeps its refusal, now reachable only when the application chose `import` and has not finished.

### D-p — Archiving stops the trunk moving, and the apply waits

⚠️ **NEW.** 0033 D-p deliberately let work already in flight finish, and that was safe because a
settle could only reach the member's own WIP branch — the trunk had `promote`, which is guarded. D-d
makes the apply the promote, so without a decision here an archived project's trunk moves hours after
it was archived, through the one path with no guard.

Refusing at settle is not available: settle is the terminal report, and refusing it leaves the row
`running` for the reaper to re-run forever.

Because D-d moved the apply out of the settle request, there is somewhere for it to wait. **The turn
settles normally, and its apply is held while the project is archived and performed on unarchive.**
Nothing is lost, nothing is stopped mid-flight, and the trunk of an archived project does not move.
The apply step is therefore also the seventh writer through `project_writable.refuse_if_archived`'s
rule — it asks the question rather than being refused by it.

## Lockstep values (new, hand-duplicated across grid-src ↔ autonomous-grid)

| Value | Direction | Absent ⇒ | Rollout order |
|---|---|---|---|
| `conversation_id` on the claim payload | relay → provider | **not a safe degrade** — wrong workspace, wrong transcript directory, that conversation permanently unresumable while everything reads healthy. The provider REFUSES, with a message distinct from the `member_key` one | **relay before the provider fleet** |
| `refs/grid/agent/` prefix | both | the provider's push is refused by the fence and the conversation never travels | **relay before the provider fleet** |
| "a transcript ref exists for this conversation" on the claim | relay → provider | the provider cannot tell "no predecessor" from "I failed to fetch it" and starts fresh silently | relay before the fleet |
| `visibility` on the project view | relay → CLI | **not private** — every relay before this ADR — so the CLI keys on an explicit value, never on falsiness | relay before CLI |
| the merge-turn marker | relay → CLI | an application renders machinery as a person's message | relay before CLI |
| `POST /tasks/{id}/turns`; the tree/blob/diff reads; the undo route | client → relay | a bare framework 404, turned into a sentence naming the relay | **relay before CLI** |
| the per-member running cap · the apply step's serialization | relay only | no half to duplicate | none |
| the delta block | — | **carries no wire value** — it rides `prompt` | none |

**Deleted, and their absence is a break rather than a degrade:** the promote route,
`promote_not_fast_forward`, the integrate route and its preview, `integrate_not_fast_forward`,
`member_has_active_task`, the promotion ledger and its read route.

## What must be measured before this is built

**TAKEN, 2026-08-14 (issue 35).** Re-runnable as `tests/measure_non_dev_design.py` in
autonomous-grid, against git 2.54.0 and Claude Code 2.1.232, on the same repository 0033 issue 16a
used (now 792 MiB / 34,159 commits / 73 authors). One row contradicted this ADR and D-c above is
amended; the rest hold.

| Measurement | What it decides | Answer |
|---|---|---|
| `git worktree` on a shared object store: is `info/exclude` in the common dir; do N worktrees fetch concurrently without lock contention | D-c's provider layout | **`info/exclude` is COMMON-directory** (a per-worktree one is not honoured, with a positive control proving the probe worked). **4/4 concurrent fetches into one object store succeeded**, no lock contention, 0.171–0.173 s. Nth worktree **+305.8 MiB** vs Nth clone **+1,043.4 MiB**. ⚠️ The old *"~12 GB"* cost was **3.28 GB** — see D-c's amendment |
| **`merge-tree --write-tree` on a large repository** | D-d's apply step and D-f's refresh — how long the apply holds its serialization | **41–43 ms** median clean across runs (2,008 commits divergent), **23.5 ms** median conflicting. The apply is **not** the grid's bottleneck: it holds its lock for tens of milliseconds |
| Does Claude Code's compaction **shorten** the `.jsonl`, and can `--resume` resume a compacted transcript after a round trip through a git ref | D-j's fast-forward rule, and whether a long conversation survives at all | **Compaction does NOT shorten it** — the file GREW 717,340 → 1,157,756 bytes across the compacting turns; it appends a summary and a boundary. **A compacted transcript DOES resume** after a real push/fetch round trip and re-materialization at a different absolute path |
| Transcript growth across ~50 turns | whether the side ref is fetched and pushed every turn or only on change | **A flat toll, not a growing one**: turn 1 costs 87,222 bytes, every turn after it ~12.8 KB (turn 2 13,672 · turn 25 12,768 · turn 50 12,851). 50 turns = **717 KB**. The per-turn ADDITION is constant; the BLOB to move grows linearly |
| The **real** tier-3 rate on a real repository with a real team | the whole cost model of D-d and D-g | **~25.0%** — 125 of 500 replayed pairs of commits by different authors within 15 minutes, sampled at an even stride over 29,553 eligible pairs (20,000 commits, 56 authors). High enough that **a merge turn is an ordinary running cost, not an edge case**. An **estimate** that OVER-counts (git-conflict ⊋ agent-unresolvable), and its method and limits travel with it |

The `1 − e^(−RT)` arithmetic that forces the apply's serialization, using 0033's own figures as the
first row:

| | main moves/hour | P(it moves during a 7-minute merge) |
|---|---|---|
| 0033 today, seven members promoting hourly | 7 | 69% *(their figure, ten-minute merge)* |
| Conservative: three members, one conversation each, a turn every 30 min | 6 | ~50% |
| Realistic: seven members, two conversations, a turn every 30 min | 28 | ~96% |
| Designed for: seven members, three conversations, a turn every 15 min | 84 | ~100% |

`max_attempts = 3` was chosen against the first row. Tiers 1 and 2 run on the bare repository with no
checkout and no agent, so under serialization only tier 3 is exposed — and tier 3 needs two people on
the **same lines**, not two people at once.

## Consequences

- **A conversation of five turns puts four intermediate states into the trunk.** D-d's accepted cost.
- **Conflict resolution is a running cost, not an event**, charged to the team's subscription and
  caused by other people's throughput.
- **`create_all` never ALTERs, renames or drops.** One live grid holds task rows, so D-a's rename and
  every new column need a hand-written, idempotent step and a test against an old-shape database. The
  failure is quiet: the master boots, then the task plane 500s while inference looks fine.
- **`main` has no known-good property left.** 0032 gave it two jobs, 0033 split them, this ADR gives
  both to the relay. Any future release gate is new work.
- **A provider writes a ref outside its own turn branch for the first time.** Narrow, per lease, and
  authorizing nothing — but it is a precedent.
- **The concurrency this ADR turns on cannot be exercised by the suite that asserts it.** The free
  cross-repo E2E runs the relay on SQLite, which serializes writers and treats `skip_locked` as a
  no-op, while the live grid is Postgres. The claim race and the apply's serialization need a
  Postgres-backed test, or their criteria pass on a database that cannot produce the interleaving.

## Out of scope (shape kept open)

- **What a non-developer takes away.** A project is a repository; the read surface covers browsing
  and downloading and nothing more. A preview, a deploy or an export is a product question, recorded
  as open rather than answered by omission.
- **Mobile.** D-m's boundary.
- **A release gate on `main`.** See Consequences.
- **Project roles that gate anything.** The column exists and nothing reads it.
- **Per-member capacity beyond D-i's cap.** `GRID_MAX_TASKS` stays per provider.
- **Renaming a conversation, and abandoning one.** A conversation has no terminal state.
- **Reverting a revert**, and undoing more than one turn at a time.
- **Cross-member transcript privacy** is no longer out of scope — D-j closes it.
