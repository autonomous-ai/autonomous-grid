---
status: proposed
---

# A project can be renamed and left, and neither is the operation it looks like

ADR 0033 gave a project its own members, and ADR 0034 made a project the thing a non-developer
actually works in. Two ordinary administrative wishes have had no answer since:

- **"I named it `scratch` and it is now my real app."** There is no rename. The nearest thing is
  `grid project create --name <new>`, which is *create-or-get by name* and therefore hands back a
  second, empty project — the silent fork ADR 0033 D-a spent a whole decision closing, wearing a
  different hat.
- **"Take me off this project."** `DELETE …/members/{member_key}` exists but calls `require_owner`,
  so only the owner may run it. A member who wants out asks the owner. There is no self-service
  path, and the three-step workaround (`member list`, find your own email, copy a 32-hex key,
  `member remove`) is one a non-developer will not find and should not need.

Both look like small CRUD gaps. Neither is, because the project plane already has three rules that
decide what these operations are allowed to mean, and each of them cuts against the obvious
implementation.

- **A project's name is unique *per owner*** — `Index("idx_projects_owner_name", "owner_id",
  "name", unique=True)`. A name was never an address (ADR 0033 D-a), and it is not the owner's
  private label either: it is a row in the owner's namespace that other people read.
- **On a grid-visible project the membership row is not what grants access** (ADR 0034 D-k, issue
  36). `project_access.reachable` resolves through the grid arm, and `key_for_or_mint` writes the
  row straight back — with an identical `member_key`, because `member_key_for` is deterministic.
  Deleting the row revokes nothing. This is measured, not feared.
- **Archiving refuses writes and leaves every read open.** The test when adding a route is *does it
  move a ref or write a row*, and that table has missed a writer twice.

## Decision

### D-a — Renaming is a write on the owner's namespace, so it is owner-only

`POST /relay/v1/projects/{project_id}/name` takes `{"name": …}`, validated by the same helper
`POST /projects` already uses — `projects.requested_name`, made public for this second consumer,
the promotion `project_view` and `require_owner` each made before it. It refuses a non-string
rather than `str(...)`-coercing it, because coercion turns `{"name": {"a": 1}}` into a project
named after a dict repr. Its own module, `project_rename.py`, beside `project_visibility.py` and
`project_archive.py`, matching `POST …/visibility`.

Owner-only, via the existing `require_owner`. The tempting alternative — any member may fix a typo —
fails on the first rule above from two directions. The name lives in the **owner's** namespace, so a
member renaming can collide with a project of the owner's that the member cannot see, producing a
refusal about something invisible to them. And on a grid-visible project every person on the grid is
auto-minted as a member, so "any member may rename" is "anyone on the grid may rename your project".

A name already taken in that owner's namespace is a `409 project_name_taken`. Renaming to the name
it already has is not an error: the unique index is satisfied by the row itself.

### D-b — Leaving gets its own route, because the caller is the one thing the token already knows

`POST /relay/v1/projects/{project_id}/leave`, authenticated as the caller and taking no body.

The obvious alternative is to relax `require_owner` on `DELETE …/members/{member_key}` to *owner or
self*. One route, one deletion path, nothing to drift. It was rejected on the **degrade shape**,
which is the axis this repo keeps losing on:

- The caller must name their own `member_key` to build the URL, and no client knows it without
  listing the members first and matching its own email. The three-step workaround survives into the
  fix meant to remove it.
- Papering over that with a `me` sentinel is worse: a **new value on an existing route**, which is
  precisely the shape that degrades silently. An old relay reads `me` as a member key and answers
  `404 no_such_project_member` — an error about a member the caller never mentioned.
- Without the sentinel, an old relay answers `403` about owners, to somebody who was not talking
  about owners.

A **new route** answers a bare 404 on an old relay, which is loud, and which the CLI already turns
into one `_OLD_RELAY` sentence naming the relay. Rollout is therefore **relay before CLI**.

⚠️ The new handler **shares** `remove_member`'s body rather than copying it: the grid-visible
refusal, the owner refusal, and the `rowcount`-based honesty about whether this request is the one
that removed anything. Two spellings of "remove a member" is exactly the two-authorization-models
failure this plane already warns about.

### D-c — Leaving means the same thing being removed means, and nothing more

Being removed today does not touch the member's running turns, their WIP branch, or their
workspace; `task_git._access` reads the membership table per request and nothing caches it, so
"removed" means *the next request is refused*. Leaving inherits that definition exactly.

The alternatives both make `leave` into something else. Cancelling the leaver's queued and running
turns turns a departure into a destructive operation and gives the plane two removal paths that
behave differently. Refusing to leave while a turn is in flight lets one stuck turn hold somebody in
a project until its deadline.

A consequence that must be said out loud rather than discovered: **a turn in a project you can no
longer reach leaves your own `grid task list`.** That is already true of removal and already pinned
by a test; leaving inherits it, so a person loses sight of their own history in that project. It is
the reason for D-f.

### D-d — Neither operation invents a way out of the owner problem

An owner cannot leave their own project. The membership table is what the fence reads, so a project
whose owner is not a member is reachable by **nobody**, and there is no adopt or transfer operation
to put anyone back. The relay already refuses this as `422 owner_cannot_be_removed`, and `leave`
reuses that refusal rather than growing a second opinion about it.

**Ownership transfer is a stated non-goal here, not an oversight.** It needs decisions this ADR does
not make: whether the recipient must already be a member, whether they must consent, and — the part
that is easy to miss — what happens when the recipient already owns a project of the same name,
because `idx_projects_owner_name` makes that transfer a unique-index violation. Transfer therefore
needs a rename-on-transfer or a refusal, which is a second feature riding on the first.

What an owner actually wants is usually one of two things that already exist, and the refusal names
both: `grid project archive` stops it accepting work and hides it from the listing, and
`grid project delete` removes it outright when it holds nothing.

### D-e — On a grid-visible project, leaving is refused rather than performed

`409 project_is_grid_visible`, the same refusal `remove_member` already gives, because the same fact
causes it: the membership row is not the grant, so deleting it revokes nothing and the next request
mints it back identically.

Two things make refusing clearly right rather than merely consistent. The relay's own note on
`remove_member` calls a 200 for an operation that revokes nothing *the worst of the three available
answers* — the caller walks away believing something happened. And here it is stronger still:
`GET /projects` is an **outer** join, and when the grid arm is open it lists every grid-visible
project regardless of membership, with `role` coalesced to `GRID_ROLE`. So leaving would not even
remove the project from the leaver's own listing. Nothing observable would change at all.

⚠️ **The message must not be `remove_member`'s.** That one tells the caller to run
`grid project private` first — an owner's operation, offered to a member who cannot run it. The
refusal a leaver gets says what is true for *them*: on a grid-visible project, membership is not
what grants access, so leaving revokes nothing, and the owner has to restrict the project before
anyone can meaningfully leave it.

### D-f — Archiving blocks the rename and permits the leave

This is the decision most likely to be "fixed" into consistency, so the reasoning is the point.

Both operations write a row, and the standing rule is that archiving refuses writes. But archiving
exists to stop **the project's content** changing while somebody has said they are done with it:

- **A rename is refused** (`project_writable.refuse_if_archived`, verb `"rename it"`). The name is
  shared metadata that other people read, an archived project is one its owner has stepped away
  from, and `unarchive` is one command away. Rename joins the `_WRITES` table in
  `test_project_archive.py`.
- **A leave is permitted.** It changes no content. Refusing it would trap a member in a project
  nobody is working in, and make them ask the owner to *unarchive* — reopening the project — purely
  so they can leave it. That is the opposite of what archiving is for. Leave is deliberately **not**
  in the `_WRITES` table, and the test that says so carries this reason, because the table's own
  rule ("does it write a row") would otherwise recruit it.

### D-g — The CLI adds no new parsed refusal code

Exactly three refusal `code`s are parsed anywhere across the two repos, and keeping the count that
low is itself the contract: every other refusal is **displayed verbatim**, because each relay
message already names the way forward and a fourth reader is a fourth thing a reworded relay could
break. `project_name_taken`, `owner_cannot_be_removed` and `project_is_grid_visible` are therefore
shown as the relay wrote them. What a test pins for them is the **remedy sentence**, not the code.

`grid project leave` takes `--yes`, matching `grid project delete`. A flag and not an interactive
prompt: a confirm that a person declines exits 0, and 0 on this plane means *done*, so a script
driving the CLI would read a refused departure as a completed one.

`grid project rename` warns — client-side, where the knowledge lives — when the old or the new name
is `default`. The projectless `grid task create` resolves the caller's own project *named* `default`
(ADR 0033 D-o), so renaming **to** `default` silently changes where the next unqualified task lands,
and renaming **away from** it makes that command start refusing. ⚠️ The relay must **not** learn this
rule: `default` is a pure client convention and the relay deliberately no longer resolves it for
anybody. Putting the check server-side would rebuild a coupling that was removed on purpose.

## Consequences

- Two new routes, both **relay before CLI**, both a bare 404 on an old relay.
- `grid project rename <id> --name <new>` and `grid project leave <id> --yes` join the application
  surface: no git vocabulary, and `--json` on both, which
  `tests/test_application_surface.py` enforces once they are added to `APPLICATION_SURFACE`.
- **A rename cannot break anyone's clone.** `grid project clone` and `grid project refresh` key
  entirely on `project_id`; the name appears nowhere in a clone's configuration.
- **A rename frees the old name, and `POST /projects` is create-or-get.** Somebody whose muscle
  memory or script still says `grid project create --name acme` will now get a new, empty project
  rather than the renamed one. This is not new behaviour and is not guarded against — it is the
  documented meaning of create-or-get — but it is the one way a rename can surprise a third party,
  so the rename's own output prints the id, which is the address that did not change.
- The three-step self-removal workaround disappears; `grid project member list` is no longer
  something a person must run in order to leave.
- Ownership transfer, and a per-member "hide this project from my list" flag, remain unbuilt and are
  now explicitly named as such (D-d, D-e) rather than looking like gaps nobody noticed.
