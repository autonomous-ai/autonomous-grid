"""Where is the grid? — resolving a training endpoint in either mode, so nobody pastes a URL.

Two deployments, one command surface (ADR 0019 D11):

* **LAN mode** — everyone in one building. The local proxy `grid up` created is the endpoint;
  nodes are reachable by address, so the trainer can push adapters straight to them.
* **Relay mode** — offices in different places. The hosted relay is the endpoint; nodes poll it
  outbound, which is what lets a laptop in another city serve without an open port.

The difference that matters for *training*, stated once here and surfaced in `doctor` and the web
interface: **adapter push needs to reach the node.** On a LAN it does. Through the relay the nodes
are behind NAT by design, so weight sync requires either nodes reachable from the trainer (a VPN,
Tailscale, same office) or the relay-side artifact plane, which is phase 2 and not in this repo.
Training still runs in relay mode; it just runs off-policy unless sync can land, and that costs
learning speed (measured: ~8× less per step at a lazy cadence).
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """A resolved training endpoint."""

    base_url: str
    mode: str            # "local" | "remote"
    label: str           # the grid's name, for display
    api_key: str = ""    # remote grids carry a bearer token; local ones don't
    can_push_adapters: bool = True
    note: str = ""

    @property
    def display(self) -> str:
        return f"{self.label} ({self.mode})"


def _local() -> Endpoint | None:
    try:
        from local import config, runtime

        cfg = config.select_grid(None)
        url = runtime.grid_url(cfg).rstrip("/")
    except (Exception, SystemExit):  # noqa: BLE001 — 'no grid configured' is an answer,
        return None                  # and the CLI helpers signal it with SystemExit
    return Endpoint(
        base_url=f"{url}/v1",
        mode="local",
        label=str(cfg.get("name") or "home"),
        can_push_adapters=True,
        note="Your machines are on this network, so trained weights reach them directly.",
    )


def _remote() -> Endpoint | None:
    """The hosted relay for the active remote grid, if the user is signed in."""
    try:
        from remote import credentials

        creds = credentials.load_credentials()
    except (Exception, SystemExit):  # noqa: BLE001 — signed out is an answer too
        return None
    grids = creds.get("networks") or creds.get("grids") or []
    if not isinstance(grids, list) or not grids:
        return None
    record = grids[0]
    for candidate in grids:
        if candidate.get("active"):
            record = candidate
            break
    token = record.get("access_token") or ""
    base = (record.get("relay_base") or record.get("base_url") or "").rstrip("/")
    if not base:
        return None
    return Endpoint(
        base_url=f"{base}/relay/v1",
        mode="remote",
        label=str(record.get("name") or record.get("network_id") or "remote grid"),
        api_key=token,
        # Relay nodes poll outbound and are unreachable from here by design.
        can_push_adapters=False,
        note=("Machines reach this grid from anywhere, which is what makes multi-office work — but "
              "the trainer cannot push new weights back to them through the relay. Keep the "
              "machines that serve attempts reachable from the trainer (same office or VPN), or "
              "expect slower learning."),
    )


def resolve(preferred: str | None = None) -> list[Endpoint]:
    """Every endpoint we can see, best first. `preferred` pins a mode ("local"/"remote")."""
    found = [e for e in (_local(), _remote()) if e is not None]
    if preferred:
        found.sort(key=lambda e: e.mode != preferred)
    return found


def describe(endpoints: list[Endpoint]) -> str:
    """One line for a CLI or a page."""
    if not endpoints:
        return ("No grid found on this computer. `grid up` starts one for this network, or "
                "`grid login` connects to your hosted grid.")
    return " · ".join(f"{e.display}: {e.base_url}" for e in endpoints)
