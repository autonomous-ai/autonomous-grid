# Packaging & distribution

How the one-line installer works and how to ship a release.

```
curl -fsSL https://grid.autonomous.ai/install.sh | bash
```

Three pieces: a **script** (`install.sh`, repo root), the **artifacts** it downloads
(GitHub Releases), and a **front door** (a custom domain that serves `install.sh`).

The installer is **hybrid by OS**:

- **Linux** → a self-contained `grid` binary (Nuitka onefile) — no Python, uv, or pip.
- **macOS** → the universal wheel installed with `uv` (the script bootstraps uv if missing).

Why not a macOS binary too? The ad-hoc-signed Nuitka onefile is **SIGKILL'd on modern macOS**
(verified on macOS 26) — distributing a macOS binary requires Apple **Developer ID signing +
notarization**, which isn't set up yet. The wheel runs fine there, so macOS uses it. When
notarization lands, add a `macos-*` binary back to the matrix and flip the macOS branch in
`install.sh`.

## Cutting a release

1. Bump the version in **both** `shared/_version.py` (what `grid --version` prints) and
   `pyproject.toml` (the wheel) to the same `X.Y.Z`.
2. Tag and push:
   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```
3. `.github/workflows/release.yml` then builds the two Linux binaries (one per runner —
   Nuitka can't cross-compile) via `packaging/build_binary.sh` plus the universal wheel, and
   publishes them with `SHA256SUMS` to the GitHub Release.

Build the Linux binary locally to reproduce CI: `packaging/build_binary.sh` → `dist/grid`.

### Release assets

| Asset | Runner | Who installs it |
|-------|--------|-----------------|
| `grid-linux-x86_64`           | `ubuntu-latest`    | Linux x86_64 (binary) |
| `grid-linux-arm64`            | `ubuntu-24.04-arm` | Linux aarch64 (binary) |
| `grid-X.Y.Z-py3-none-any.whl` | (release job)      | **macOS** (`uv tool install`), and any uv/pip user |
| `SHA256SUMS`                  | (release job)      | integrity check the Linux path verifies |

Drop a row from the matrix in `release.yml` to skip a Linux arch; the installer reports
"no build for this OS/arch" if a user is on one you don't publish.

## Front door (custom domain)

GitHub Release URLs are stable but ugly. Serve `install.sh` from `grid.autonomous.ai` so the
one-liner reads cleanly (same pattern as `astral.sh/uv/install.sh`). Easiest with a
**Cloudflare Worker** that proxies the raw file from this repo:

```js
// Worker bound to the route  grid.autonomous.ai/install.sh  (and  /  )
export default {
  async fetch() {
    const raw = "https://raw.githubusercontent.com/autonomous-ai/autonomous-grid/main/install.sh";
    const r = await fetch(raw, { cf: { cacheTtl: 300 } });
    return new Response(r.body, {
      status: r.status,
      headers: { "content-type": "text/x-shellscript; charset=utf-8" },
    });
  },
};
```

DNS: add the `grid` hostname (proxied/orange-cloud) in Cloudflare, then attach the Worker
route `grid.autonomous.ai/*`. Test with `curl -fsSL https://grid.autonomous.ai/install.sh | head`.

**Alternative — GitHub Pages:** commit `install.sh` to a Pages branch/dir, set the custom
domain `grid.autonomous.ai` (CNAME), and it serves at `grid.autonomous.ai/install.sh`. No Worker, but
a propagation/cache lag on updates.

## Security notes

- Always serve over **HTTPS**; the Linux path forces `--proto '=https'` on every download.
- The **Linux** path **verifies SHA-256** against the release's `SHA256SUMS` and hard-fails
  on mismatch — the same integrity check `deliver-grid.sh` does over SSH. The **macOS** path
  installs the wheel over HTTPS via `uv tool install`.
- Pin a version for reproducible installs: `GRID_VERSION=0.1.0 curl … | bash`.
- For extra hardening, pin the GitHub Actions in `release.yml` to commit SHAs.
