# A grid can be keyed on an OS, and that grid is a public one

Status: accepted (2026-08-28)

`private-domain` (grid-apis `store.NETWORK_TYPE_PRIVATE_DOMAIN`) auto-provisions one grid per email
domain and admits every account of that domain with no allowlist row. It works because a shared email
domain is a *proxy for a company* — people who already trust each other.

We now want the same "you already belong somewhere" experience keyed on the **operating system** a
person runs, so that somebody with no company domain still lands in a grid where compute is being
shared. This ADR records that the shape may be copied but the **premise may not**: same OS implies no
trust relationship whatsoever, and three separate mechanisms in the system today are built on the
premise that it does.

## D-a — The type is `os-community`, and it is a community pool for inference

One new `network_type`, `os-community`, whose purpose is **sharing inference compute among strangers
who run the same OS**. Not an onboarding default that happens to be public, and not a compute-
compatibility filter — those were the two alternatives, and each would have produced a different
design (see *Considered options*).

The literal deliberately avoids `private` and `restricted`. `private-domain` is not private — it is
open to a whole domain — and that misnomer already cost an ADR 0034 D-k correction (`project_access.py`
records it: the claim that "grid *is* company" is true of the domain and false of the roster). Reusing
either word on a grid that is genuinely open to strangers would repeat that mistake with higher stakes.

## D-b — The OS gate is a filter, not a security boundary

The CLI reports its own OS. The control plane cannot verify it. This is the same class of value as
`device_id`, which is a `uuid4` the CLI generates and writes to `~/.grid/device.toml`
(`remote/credentials.device_id`).

Written down because the gate looks like an access control and is not one: it stops *mistakes*, not
*intent*. Nothing downstream may be justified by "only macOS machines are on the macOS grid".

## D-c — The taxonomy is closed, and `other` never becomes a grid

Exactly four tokens: `macos`, `windows`, `linux`, `omarchy`. An unrecognised Linux resolves to
`linux`; anything else resolves to **nothing**, and no grid is provisioned.

The closed set is forced by D-d: auto-provisioning on an open value space means every unrecognised
string becomes a permanent empty grid. `shared/system/host.platform_kind()` already carries an `other`
bucket for exactly this reason, and `other` must never reach this path.

⚠️ **Omarchy is Arch-based, and whether `/etc/os-release` distinguishes it from stock Arch is
UNMEASURED.** `shared/engine/installer._detect_distro` reads `ID=`/`ID_LIKE=` and is the shape to
follow, but if Omarchy reports `ID=arch` then a second signal is required. Measure on a real Omarchy
box before implementing; do not infer it.

`platform_kind()` was rejected as the vocabulary even though it already exists and is already on the
wire (`remote/serve.py` heartbeat `load["platform"]`). It answers *"which binaries run here"* — a
compute question — and reusing it would split the macOS community across two grids by CPU generation
(`macos-arm64` / `macos-x86_64`). Two questions, two vocabularies, even where today's values overlap.

## D-d — Auto-provisioned, owned by a configured system account

The grid is claimed on first sighting like `private-domain`, but `owner_google_sub` comes from
configuration, **never from the first account seen**.

`private-domain` makes the first account of a domain the owner, and an owner is not a formality:
`handler._require_managed_creator` makes them the only account that may start, stop, delete, rename or
re-type the grid, and `store._owner_member` grants them `roles=["admin"]`. On a domain that is a
colleague. On an OS grid it would be the first stranger in the world to run that OS, holding delete
rights over everybody else's grid.

## D-e — Membership is stateless, computed from the OS claimed on `GET /tokens`

Nothing about a person's OS is persisted. The CLI sends `os=` alongside `device_id` on
`GET /v1/grid/tokens`, and that request's value decides which OS grid it receives a token for.

The alternative shapes were rejected on modelling grounds. A domain is **derivable from stored data**
(`grid_users.email`), which is why `store._domain_member` can synthesize membership from
`(network, email)` alone and `list_domain_member_emails` can build a roster in one query. An OS is
derivable from nothing we store, and `grid_network_devices` cannot help because it is keyed on
`network_id` — you would have to already be a member of the macOS grid to be found as a macOS user.
Storing one OS per account (`grid_users.os`) would force a many-valued fact into one cell, and its
symptom is a person's grid changing every time they use their other machine.

Two consequences are deliberate, not oversights:

- **The roster carries nobody the OS gate admitted.** No member admitted by the OS gate holds an
  allowlist row and none can be synthesized, so none of them appears in `list_grid_members`. On a grid
  of strangers that is what matters — it must not publish ten thousand people's email addresses.
  (Note that `handler._require_managed_member` otherwise lets *any* active member read the roster and
  invite.)

  ⚠️ **Not literally "reports nobody", and the earlier wording here said so.** The grid's own
  **owner** — the platform account that created it — holds an ordinary allowlist row like any other
  grid's owner, and the roster returns it: `test_the_roster_of_an_os_grid_reports_nobody_and_that_is
  _deliberate` asserts `== ["platform@autonomous.ai"]`, one row, not zero. The claim worth making is
  the one the test actually proves, and it is the load-bearing one; "empty" was a shorter sentence
  that would eventually be read as an invariant and relied on. Corrected 2026-08-28 after review.

  ⚠️ **The roster is not the only surface that would publish those addresses.** The member-usage
  panel reaches the same emails by a different mechanism — observed traffic, not the allowlist — so
  this bullet's reasoning does not close it. It is closed separately, in D-l.
- **The grid exists only in the CLI.** `grid ls` reads the locally-stored list that `GET /tokens`
  filled (`cli/remote_grid.cmd_remote_ls`, `remote/control_plane.fetch_tokens`); a browser session has
  no CLI OS to report, so `/me` and `/networks` show nothing. The app is out of scope for this feature
  and this is what pays for the small design.

The membership fact is therefore about a **session on a machine**, not about a person: *you are a
macOS user for as long as the machine you are typing on is a macOS machine.*

⚠️ **A third consequence, found while building issue 02 and since resolved (see the decision at the
end of this section): the refresh-credential path carried no OS claim, so it could not renew an OS
grid's token.** `POST /v1/grid/tokens/{id}` with a `refresh_token` re-resolves the caller through
`store.member_for_access`, and on this type that call had nothing to match on — so it answered 403
*"Member is not active"*. `GET /tokens` still issued a bundle whose `refresh_token` was therefore
inert on this one type. The account below is kept in the past tense on purpose: it is the reasoning
the decision rests on, and a later reader deciding whether to simplify the wire needs it.

It bites where the CLI refreshes by itself rather than by a person's command: `remote/serve.py`'s
serve loop and `cli/grid_credential.py` both exchange the refresh credential on a 401, and a 401 is
what every `network_epoch` bump produces. The access token's own TTL is a year, so this is not a
routine expiry — it is what happens the first time an OS grid's epoch moves under a machine that is
serving on it. The recovery exists and is a person's command (`grid sync` re-fetches with `os=`).

⚠️ **This paragraph originally ended "which is why this is a gap rather than a break". That was
wrong, and the correction matters more than the original claim.** The refusal is **not read-only**:
`handler.refresh_token` calls `store.rotate_refresh_credential(...)` — which commits an `UPDATE`
replacing `refresh_token_hash` — *before* it consults `member_for_access`. On this type that check
is guaranteed to fail, so the credential is rotated away, the replacement is generated, and the 403
discards it. The CLI persists nothing on a failed refresh, so the machine is left holding a token
the server no longer knows: every later attempt answers `401 "Invalid or expired refresh
credential"`, not this 403, and **shipping the `os=`-on-refresh fix does not heal a machine it has
already happened to** — only a person re-running `grid login`/`grid sync` does. "The recovery
exists" was reasoning about a refusal that does not consume what it refuses; this one does.

That makes the ordering a defect in its own right, independent of the wire shape chosen below: a
refusal on this route must not spend the credential it is refusing, on **any** network type. This
type merely reaches the path every single time, which is why it surfaced here.
`.scratch/os-grid-type/issues/10-an-os-grids-credential-can-be-renewed.md` carries both halves.

**DECIDED 2026-08-29 (issue 10): the refresh request carries the `os` claim too.** Two shapes were on
the table, and the second turned out not to solve the problem at all:

- **Chosen — carry `os` on the refresh request.** The request stays the whole membership fact, so D-e
  is untouched and the gate on renewal is the same equality test as the gate on sign-in. The cost is
  a second wire value in a second place, which is paid for by pinning it: the fetch's query parameter
  and the renewal's body key are two independent spellings and get two independent lockstep cases,
  plus a third asserting this CLI's own two call sites agree with each other.
- **Rejected — let a refusal on this type mean *re-sync* rather than *end of run*.** It reads cheaper
  (no new wire value) and it is not: **it cannot deliver unattended renewal, which is the entire
  point.** `grid sync` needs a session token, and `SESSION_TTL_SECONDS` is 24 hours — so a provider
  running longer than a day still ends at a human with a browser, which is exactly the state being
  fixed. It also has to tell this refusal from a genuine loss of membership, and the only local
  signal is the stored `network_type`, which `cli/grid_credential.py` already records as *"a snapshot
  from the last login/sync that nothing refreshes on a token exchange"* — stale precisely on a grid
  that was re-typed. Doing it on a wire `code` instead is worse: exactly three are parsed across
  these seams and keeping that count low is the contract.
- **Still forbidden — persisting the OS the bundle was minted with.** The stored-`grid_users.os`
  shape rejected above, arriving through a side door. Pinned by
  `test_the_os_a_bundle_was_minted_with_is_never_persisted`, which renews twice on one OS and then
  claims another: if anything anywhere had remembered the first answer, that call would succeed.

⚠️ **The chosen shape has no rollout ordering in either direction, and that was MEASURED rather than
assumed.** `TokenRefreshRequest` does not forbid extra fields (Pydantic's default is `ignore`), so a
newer CLI against an older control plane has the key dropped in silence and behaves exactly as it did
before; an older CLI against a newer control plane sends nothing and likewise. The check mattered:
the sibling model in these repos, grid-src's `task_files.parse_files`, **refuses** unknown keys and
answers 422 — the same change against that model would have been a break, not a degrade.

## D-f — `name` is the label, `access_os` is the gate, the unique index is on `access_os`

`name` carries the human label (`macOS`), a new `access_os` column carries the gate token (`macos`),
and the partial unique index that makes the auto-provision claim atomic sits on **`access_os`**, not on
`name` (compare `uq_grid_networks_private_domain_live`, which sits on `lower(name)`).

`private-domain` makes `name` do two jobs, and `store.domain_gate_for` exists only to hide the fact
that the two domain types keep their gate in different places. Not repeating that here means a user who
names their own grid `macos` collides with nothing.

## D-g — Inference on an `os-community` grid is free, and stays free

`relay._billing_on` (grid-src) opens with `if config.grid_network_type != "permissioned-providers":
return False`, and grid-apis `store.set_network_billing_mode` says the same ("effective only on
permissioned-providers"). We are not widening it. Consumers pay nothing and providers earn nothing.

Widening it would mean opening a seam that ADR 0034 D-i and issue 53 already sit on, plus wallets and
balances for strangers. Free is the only behaviour that works without touching it, so free is what we
ship and what this ADR commits to.

## D-h — The denylist IS widened to this type

`store._is_denied_consumer` and grid-src `grid_auth._is_open_consumer` both open by testing for
`permissioned-providers`; the latter's docstring states "Denylist enforcement is scoped to this type
only". Both are widened to admit `os-community`.

This is the one enforcement mechanism we extend, and the reason is asymmetric with D-g: without it
there is **no way at all** to stop an abusive account on a grid that admits strangers automatically,
and the only remaining remedy would be taking the whole grid down. ⚠️ The predicate as written only
bites a token whose roles are exactly `{"consumer"}`; on this type members are `both`, so the role
condition has to be re-derived rather than copied.

## D-i — The task plane is refused at the door on this type

**Project creation** is refused on an `os-community` grid, and that single refusal is the whole
plane's gate. A project is never minted as a side effect — `projects.ensure_project` is the only
thing on the relay that writes a `ProjectRow` and `POST /relay/v1/projects` is its only caller (a
projectless task create is *refused*, not helped, since ADR 0033 D-a). So with creation refused no
project can ever exist on such a grid, and every task create, turn create, commit, read and stream
downstream is addressing a project that is not there and answers exactly what it already answers for
one — no gate on each of the thirty-odd modules that make the plane up, and no hand-maintained table
of which routes need one.

⚠️ **This paragraph previously read "Project creation and task creation are refused", which was
wrong in a way worth recording: it describes the OUTCOME (the task plane is unusable) as though it
were the MECHANISM (two gates).** Issue 05's acceptance criterion 3 requires a task create on such a
grid to answer the ordinary project-not-found 404 — asserted, so that the "nothing downstream needs
a gate" claim is checked rather than assumed — and a second refusal on `POST /tasks` would both
contradict that and start the per-route list this decision exists to avoid. The reasoning sentence
that follows the claim always argued for one gate; only the claim sentence was loose. Amended
2026-08-28 when issue 05 was built, after the conflict was flagged rather than silently reconciled.

⚠️ **The premise "because the type is new there is no existing project anywhere on it" is not
strictly true, and the residue is known.** A grid *re-typed* onto `os-community` returns after the
restart carrying whatever projects it already had — grid-apis refuses that re-type in both
directions (issue 02), but grid-src's own `VALID_NETWORK_TYPE_INPUTS` still admits it, so a
self-hosted operator can reach the state. What survives is: no NEW project can be created (403), a
stranger gets the project-shaped 404 because `grid_access_enabled()` is False on this type and
nothing is grid-visible, and only a pre-existing project still serves the members whose rows already
exist.

⚠️ **Those rows are NOT "access the owner granted by hand", and an earlier draft of this paragraph
said they were.** On the likeliest source grid — a `private-domain` one, where `grid_access_enabled()`
is True — a membership row is **self-minted on read**: `project_access` admits any authenticated
caller to a grid-visible project (`:295`) and then writes the `ProjectMemberRow` (`:401`). So the
surviving cohort is *everyone who ever opened a grid-visible project on that grid*, including the
invited outsiders D-k admits, who are on other email domains entirely. That is a materially larger
and less deliberate set than "the people the owner admitted", and it is the reason issue 09 is worth
doing rather than filing as a curiosity. Corrected 2026-08-28 after review. Pinned by
`TestTheOnePlaceTheSingleGateDoesNotReach` rather than left to be rediscovered. Closing it is a
separate slice — see `.scratch/os-grid-type/issues/09-the-grid-src-re-type-door-onto-an-os-grid.md`.

grid-src's `task_domains` — the mechanism built to stop a provider receiving a checkout of somebody
else's repository — is an **email-domain allowlist**, and its own docstring explains why it cannot serve here:
"two people on `gmail.com` are not one company". Unset it filters nothing at all ("Unset ⇒ nothing is
filtered"), so on a grid with no domain it is not a weak control, it is an inapplicable one.

Task serving is already opt-in and off by default (`remote/task_opt_in.serving_enabled`, `GRID_TASKS`),
so nobody is exposed today who did not switch it on. That is a brake, not a boundary, and the plane's
value — projects, git, agents — is a *team collaboration* feature that means nothing among strangers.
Refusing it takes nothing away from D-a and is the narrowing direction.

⚠️ grid-src's `project_access.grid_access_enabled()`
(`grid_cli/private_server/project_access.py` — **not** this repository, which has a same-named
module of its own) gates grid-wide project visibility on `private-domain` and
must **never** be widened to `os-community`. D-i makes it moot, but the test that pins it is what stops
a later reader "completing the set".

## D-j — grid-src rolls out BEFORE the control plane

The opposite of the ordering that applies to the web-tool routes, and it must be written down or it
will be inferred wrongly from the neighbouring case.

Auto-provisioning is not an INSERT: grid-apis `managed_networks.build_create_argv` shells out
`grid network create <name> --network-type os-community …`, and that `grid` binary **is grid-src**.
grid-src validates with `choices=VALID_NETWORK_TYPES` (`grid_cli/cli.py`), so a control plane that
knows the type before grid-src does gets argparse exit 2, a failed create, and a grid stuck at
`pending` retrying forever. Loud and fail-closed, but wrong.

⚠️ **That name is a shadowing alias, and following it to the wrong set leaves the failure in
place.** `grid_cli/cli.py` opens `VALID_NETWORK_TYPES = network_runtime.VALID_NETWORK_TYPE_INPUTS`,
so argparse's choices are the **INPUTS tuple** and not `network_runtime.VALID_NETWORK_TYPES` beside
it. Both need the token, and so does `PUBLIC_NETWORK_TYPES`: `cmd_network_create` refuses
`--advertise-url` on a non-public type and `build_create_argv` always passes that flag, so a type
missing from the public set fails this same create one layer further in.

The public CLI has **no** ordering in either direction: `os=` is a new query parameter on an existing
endpoint, so an old control plane ignores it silently, and a new control plane facing an old CLI simply
issues nobody a token.

## D-k — Absence is reported, because three causes share one symptom

An empty `grid ls` can mean the CLI is too old to send `os`, the control plane has the feature off, or
the machine's OS is outside D-c's set. The CLI reports the third itself (it knows its own OS and the
set of tokens it can emit, so no round trip is needed), and `GET /tokens` carries an `os_served` key
for the other two.

`os_served` is a new key on an existing endpoint and therefore degrades **silently** against an old
control plane — the key is absent and the CLI prints nothing, which is exactly the previous behaviour,
so the degrade is clean.

## D-l — The member-usage panel is refused on this type

`GET /relay/v1/grid/members/usage` (grid-src `relay.py`) returns one row per consumer: the person's
email alongside their `requests` / `tokens_in` / `tokens_cached` / `tokens_out`. Its gate is
`_extract_auth(request, "inference:models")` — the scope every consumer needs to use the grid at all —
so on a grid that keeps no allowlist it reduces to *"do you hold a token for this grid"*. That is the
right gate on the types it was written for, and its docstring says why: requiring a snapshot row
unconditionally would lock every caller out of a grid that keeps no roster. On `os-community` the same
sentence means **every stranger in the world running this operating system**, so the panel would hand
each of them every other one's email address and spend.

⚠️ **D-e does not already cover this: same concern, second mechanism.** D-e's roster answer holds *by
construction* — nothing about a person's OS is persisted, so `list_grid_members` has no rows admitted
by the OS gate to return. This route's emails do not come from the allowlist. They come from
`node_answered_query.member_snapshot()`, traffic the relay actually observed, which no membership
model can empty. Widening `_requires_allowlist` to this type (issue 01) is what makes the route
reachable here, and a reader who stops at D-e will conclude the surface is closed when it is not.

⚠️ **The app being out of scope is not the containment it looks like.** D-e says an OS grid exists
only in the CLI, and `member_usage_provider.dart` is this route's only reader — but the exposure is the
*route*, not the panel. Any token holder can call it directly, and on this type every member is a token
holder. A UI that never renders it is not a gate.

**DECIDED 2026-08-29 (issue 08): the route is refused on `os-community`, at the door.** The same shape
as D-i and for the reason that worked there — one predicate keyed on the type, rather than a per-caller
filter that has to stay correct forever. Nobody on a grid of strangers has a reason to audit another
stranger's spend, so the refusal takes nothing away from anybody. Three costs, stated rather than
hidden:

- One more network-type predicate to keep in step with the others in this ADR.
- It must be keyed by **equality** against the `os-community` literal, never on the truthiness of a
  missing key — the standing rule of the lockstep register, which has five entries that exist because
  of that one mistake.
- The pin needs a **positive control**: a test proving the route still serves a `private-domain` grid
  unchanged. Without it the refusal is equally satisfied by a route that has been switched off for
  everybody.

The alternatives, and what each would have cost:

- **Return the caller's own row only.** Keeps the endpoint uniform and the app's parsing untouched, and
  costs a response *shape* that varies by network type — the sort of thing a later reader reports as a
  bug, on a path no test in the app exercises.
- **Drop the email and report totals only.** Preserves *how busy is this grid* without naming anybody,
  and costs an absent email every client must render, plus the docstring's promise that the panel
  reading this "has the roster keyed by email already".
- **Decide the exposure is acceptable and say so.** Legitimate on its face — the figures are token
  counts, not content — but it trades ten thousand strangers' email addresses for a panel that, on this
  type, answers a question nobody is asking.

⚠️ **D-l is not the last word on this exposure either, and its own review proved it.** The public
overview publishes `provider_email` with no gate at all; that is a THIRD mechanism, closed separately
in D-m. A first draft of this route's refusal sent the refused caller *to that overview* for "the
grid's own totals, naming nobody" — false, and a good illustration of how easily this area is
reasoned about one surface at a time.

There is **no rollout ordering** and no half in this repo: the refusal changes no response on any other
type, and the app is unchanged because D-e keeps an OS grid out of its reach entirely. If the app is
ever brought into scope, this refusal is one more thing to revisit alongside D-e, not a thing that will
already be right.

## D-m — The public overview names no provider on this type

`GET /relay/v1/grid/overview` is **public and unauthenticated** — its own docstring says so, and it
takes no `_extract_auth` at all. For every node whose role is `provider` or `both` and whose
heartbeat is inside the TTL, `overview.build_grid_overview` emits `provider_email`, joined from
`UserRow.email`, beside the machine's name, chip, VRAM and the models it serves.

On a `private-domain` grid that is a company publishing its own people's machines, which is what the
web UI was built for. On `os-community` every member is minted `both` and the type's whole premise is
that strangers share compute — so anyone who knows the grid's signaling URL can read the address of
everybody serving on it, with no credential at all.

⚠️ **This is the THIRD mechanism onto one exposure, and each of the first two was closed by an
argument that does not reach it.** D-e empties the roster *by construction* — no OS membership is
persisted, so `list_grid_members` has no row to return. D-l refuses `/grid/members/usage`, whose
emails come from observed traffic rather than the allowlist. This one reads `NodeRow` joined to
`UserRow`: rows a provider writes by the act of serving, on a route with **no gate whatsoever**. So
the door left open was the *weakest*-gated of the three, which is not a decision anybody made — it is
the shape of finding the mechanisms one at a time.

**DECIDED 2026-08-29 (issue 14): `provider_email` is sent as `null` on `os-community`, and the key
stays.** Keyed by equality against the type literal, the same shape as D-l and D-i.

- **The key stays and the value goes.** Omitting it would make the payload's *shape* vary by network
  type, which is the cost D-l explicitly rejected when it turned down "return the caller's own row
  only" — a shape that differs per type is the sort of thing a later reader reports as a bug.
- **Overloading `null` is deliberate.** It already means "the control plane could not resolve the
  owner", and a client cannot tell that from "withheld". Nothing should: both render as a machine
  nobody is named for, and a distinguishable "withheld" would advertise that there is a name to ask
  for.
- **No app half, measured rather than assumed.** `OverviewNode.providerEmail` is already
  `String?`, parsed as `j['provider_email'] as String?`, and its own doc comment says "Null on an
  older relay, or when the control plane couldn't resolve the owner". `nodeHostHandle` returns `''`,
  and `node_groups` collects every unattributed machine into **one headless block** — a designed-for
  case, not a degenerate one. So there is no rollout ordering in either direction.

Three alternatives were on the table, and each was rejected for a stated reason:

- **A stable non-identifying handle** (the node name, or a digest of the address). Keeps "which
  machine is this" while dropping the identity, and costs a second spelling of who a provider is. The
  deciding objection is that a digest which is stable across polls — and it must be, or the machines
  regroup on every refresh — is a durable pseudonymous identifier, which is a different privacy
  claim from the one the word "anonymous" makes.
- **Authenticate the overview on this type.** The narrowest change to the payload, and it is the gate
  the sibling route already has. Rejected because on this type **every member is a token holder**: it
  converts an unauthenticated leak into an authenticated one, which is precisely what D-l refused to
  accept for `/grid/members/usage`. It would also take the public dashboard away from the one grid
  type whose whole point is that strangers can find it.
- **Accept the exposure and say so.** Defensible on its face — somebody serving on a public pool
  arguably publishes themselves by serving. Rejected because they do not: a person runs `grid join`
  on a machine, and nothing in that act says their email address becomes readable by anyone on the
  internet who knows a URL. ⚠️ The app already knew and said so — `node_display.nodeHostHandle`
  carries the comment "Not a privacy measure and shouldn't be mistaken for one: `/grid/overview` is
  unauthenticated and carries the whole address." A relay-side decision was the missing half, not a
  finding nobody had made.

⚠️ **The rule this ADR keeps re-learning, written once here.** Three surfaces have now published a
member's identity on this type, and in each case the previous decision looked like it covered the
next. Before adding a route that reads `UserRow.email`, or widening one, the question is not *is this
like the roster* — it is *what does this surface publish on a grid of strangers, and what is its
gate*. The predicate is shared for this reason: `member_identity_access` owns both answers, so the
fourth surface finds one module rather than two conventions.

## Considered options

- **An onboarding default (auto-join, private-domain shape, for everyone).** Rejected: it reintroduces
  precisely what `domain_networks.private_domain_blocklist` exists to prevent — its comment names the
  case, "so `k@gmail.com` does not create a grid shared by every Gmail account" — at a larger scale,
  while inheriting a trust premise that does not hold.
- **A compute-compatibility pool.** Rejected as a *grid*: the platform of every node is already on the
  wire in the heartbeat, so grouping by it is a routing question, not a new membership boundary.
- **Reusing `permissionless` plus a discovery rule** (no new type at all). Rejected for one concrete
  reason: `store.list_visible_networks` makes every `permissionless` grid visible to every account, so
  each person's `grid ls` would grow by one line per OS forever.

## Consequences

- One more hand-duplicated wire literal across three repos (grid-apis `store.py`, grid-src
  `network_runtime.py` and `grid_auth.py`), with a rollout order that is the reverse of its neighbours.
- A grid type on which billing is permanently off by decision rather than by omission (D-g).
- One route refused and one payload field withheld purely because the grid admits strangers (D-l,
  D-m), and a standing question for every future surface that reads `UserRow.email`: what does it
  publish on a grid of strangers, and what is its gate? Three surfaces have now had to be answered
  one at a time; `member_identity_access` exists so the fourth finds one module rather than two
  conventions.
- A grid type with no roster and no presence outside the CLI (D-e) — acceptable only while the app
  stays out of scope. Bringing the app in later means revisiting D-e first, not bolting a query on.
- `grid ls` gains a `os-community` row that most users will never have created, and the type string is
  printed to them verbatim (`cli/remote_grid.cmd_remote_ls`).
