# Connect to or operate a self-hosted relay

A Grid may use an Autonomous-hosted relay or a relay run by its owner. Goals and tasks still belong
to the Grid; they do not store a second relay id. The Grid's local credential record determines
which relay every inference, task, Git, and Goal request uses.

## Join an owner-operated Grid

Ask the relay owner for a pairing bundle, transferred through a secure channel, then run:

```bash
grid relay connect --bundle-file /secure/path/worker.pairing
grid use <grid-name>
grid relay info <grid-name>
```

For an interactive paste, omit both bundle flags; input is hidden. `--bundle` exists for automation
but exposes the credential in shell history. Connecting switches this machine to remote mode and
selects the paired Grid. It does not require an Autonomous account or hosted control plane.

`grid login` and `grid sync` preserve self-hosted Grid records. A new pairing bundle refreshes or
replaces the credential for the same Grid. To forget it locally without stopping the relay:

```bash
grid relay disconnect <grid-name>
```

## Host commands

Relay hosting is a separate runtime because most Grid machines should be clients/workers, not
authorities. Install the `grid-relay` package on the host. These public CLI commands delegate to
that executable without a shell:

```bash
grid relay up ...
grid relay list --health
grid relay status ...
grid relay invite ...
grid relay revoke ...
grid relay set-url ...
grid relay backup ...
grid relay restore ...
grid relay service install ...
```

The operator runbook, including TLS, auto-start, restore, and destructive teardown, ships with the
relay/server repository. Client-only nodes need only `grid relay connect`.
