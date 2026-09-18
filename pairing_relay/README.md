# Grid pairing relay — minimal cell

A phone and a desktop that both dial **out** to this process end up talking to
each other. That is the whole service: it exists because neither of them can
accept an incoming connection, and it is deliberately unable to read a single
byte they exchange.

```
   phone  ──wss──▶  /v1/connect/<relayHostId>  ─┐
                                                ├─▶  splice: bytes, verbatim
 desktop  ──wss──▶  /v1/host/data/<connId>     ─┘
 desktop  ──wss──▶  /v1/host/control              (long-lived, one per desktop)
```

Not a port of Orca's relay — a minimal rebuild of the mechanism, in this repo's
stack, small enough to read in one sitting.

## Run it

```sh
python -m pairing_relay --port 8787
pytest pairing_relay          # 42 tests, ~4s, no network
```

To see the whole thing work — a desktop proving a host id, a phone dialling in
with a pairing code, and an end-to-end encrypted channel built *through* the
splice — start the cell above and run the driver in the Flutter app repo:

```sh
dart run tool/pairing_relay_probe.dart        # autonomous-grid-app
```

It plays both halves against this process and fails loudly on the first thing
that disagrees. That probe is the only check that spans the two repositories:
nothing in either test suite can see the other, so the agreement between this
cell's `host_proof.py` and the app's `relay_host_proof.dart` is proven by
running it, not by CI. Same standing as the panel firmware.

## How one connection happens

1. **Desktop opens `/v1/host/control`** and sends `host-hello` carrying its
   X25519 public key. The cell derives `relayHostId` from that key — it does
   not take the desktop's word for it — and refuses a hello that claims any
   other id.
2. **The cell challenges it.** It seals a random secret *and a transcript* to
   the claimed key. Only the real holder can open it; and because the
   transcript names the origin, the id, the key and the window, the desktop can
   check it is not being asked to sign for a relay it never dialled. The
   desktop answers `HMAC(secret, transcript)`.
3. **Desktop asks for an invite** (`invite-create`); the token goes into the
   pairing code the phone scans.
4. **Phone opens `/v1/connect/<relayHostId>`** and presents the token. The cell
   reserves it, mints a `connId` and a `connTicket`, and pushes `conn-open`
   down the desktop's control channel.
5. **Desktop opens `/v1/host/data/<connId>`** and presents the ticket.
6. **The cell wires forwarding, and only then tells the phone it is through.**
   That order is not cosmetic — see `splice_state.may_acknowledge_client`.

From here every frame is copied across untouched. The two peers run their own
end-to-end encryption on top (`lib/infrastructure/pairing/` in the app), so
this process handles ciphertext exclusively and could not eavesdrop if it tried.

## The parts worth reading

| File | What it decides |
|---|---|
| `splice_state.py` | The order a connection may happen in. Pure. |
| `host_proof.py` | Who owns a `relayHostId`. Both halves of the protocol. |
| `cell.py` | Sessions, invites, the splice, and every limit. No transport. |
| `server.py` | FastAPI adapter. Thin on purpose. |

## Three decisions that differ from Orca, and why

**No send queue.** A forward is `await peer.send(...)`, so a slow phone slows
the desktop's reader and nothing accumulates: memory is one frame per
direction. Orca's cell carries a queue and a byte budget because Node's
`ws.send` buffers without awaiting — asyncio gives that back for free. What
*does* have to be built is the other half, a peer that never drains, and that
is `splice_send_timeout_s`.

**A host id is derived from a key, not issued.** No registry, no assignment
table, nothing to race or reconcile. Claiming someone's id means holding their
private key. Orca needs an issued id because it spans many cells and has to
know which one a desktop landed on; a single cell does not.

**Two close codes name a cause, not one.** `HOST_OFFLINE` means no desktop is
connected; `ATTACH_TIMEOUT` means one is, but did not pick up. Orca spends
`4404` on both and its own comment admits the second is the first wearing a
borrowed name. A phone that cannot tell them apart has to guess which advice to
show its owner.

## ⚠️ What this is not

Everything below is load-bearing in production and absent here on purpose. The
list is the point of the exercise — it is what "minimal" actually cost.

- **No director, no multi-cell.** One process holds every session in memory. A
  restart drops every pairing, and there is no assignment, no migration, no
  `/v1/assign`, no `/v1/resolve`, no regional placement.
- **No resume credential.** An invite opens exactly one connection and is then
  spent, so a phone that loses its network has to be paired again. Orca's
  30-day rotating resume token is the single biggest omission, and it is what
  makes the difference between a demo and something a person would use.
- **No authorization.** The proof answers *who*, never *whether*. There is no
  account, no quota, no revocation, no per-IP admission — anyone who can reach
  the port can register a host id. Orca carries a signed `relayJwt` for exactly
  this, and it is not optional.
- **No persistence and no observability.** No database, no metrics, no
  structured logs, no draining, no graceful rollout.
- **`ws://`, not `wss://`.** Put TLS in front of it. The end-to-end layer means
  a passive listener still reads nothing, but the invite token is in the clear
  on the wire without it, and that token is authority.
