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

- **The roster is empty by construction.** No member holds an allowlist row and none can be
  synthesized, so `list_grid_members` reports nobody. On a grid of strangers that is correct — it must
  not publish ten thousand people's email addresses. (Note that `handler._require_managed_member`
  otherwise lets *any* active member read the roster and invite.)
- **The grid exists only in the CLI.** `grid ls` reads the locally-stored list that `GET /tokens`
  filled (`cli/remote_grid.cmd_remote_ls`, `remote/control_plane.fetch_tokens`); a browser session has
  no CLI OS to report, so `/me` and `/networks` show nothing. The app is out of scope for this feature
  and this is what pays for the small design.

The membership fact is therefore about a **session on a machine**, not about a person: *you are a
macOS user for as long as the machine you are typing on is a macOS machine.*

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

Project creation and task creation are refused on an `os-community` grid. Because the type is new
there is no existing project anywhere on it, so refusing the entry points refuses the whole plane
without a gate on each of the thirty-odd modules that make it up.

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
- A grid type with no roster and no presence outside the CLI (D-e) — acceptable only while the app
  stays out of scope. Bringing the app in later means revisiting D-e first, not bolting a query on.
- `grid ls` gains a `os-community` row that most users will never have created, and the type string is
  printed to them verbatim (`cli/remote_grid.cmd_remote_ls`).
