"""Keep the CLI itself current: the background version check, the stale notice, `grid update`.

Three pieces, one flow (the uv/pip shape, adapted to how this CLI ships):

1. **Background check.** Any ordinary command that is due (cache older than ``CHECK_INTERVAL_S``)
   spawns a detached ``grid __update-check`` — the same self-exec pattern as ``__server`` — which
   resolves the newest release tag and writes ``~/.grid/update-check.json``. The user's command
   never waits on the network; discovery costs zero latency.
2. **The notice.** After the command's own output, one line on **stderr** when the cached newest
   version is newer than the running one — at most once per interval. Suppressed for ``--json``,
   non-TTY stderr, ``__*`` internals, dev builds, ``grid update`` itself, and
   ``GRID_NO_UPDATE_CHECK``. So it rides whichever command the user actually runs — `login`, `ls`,
   `models`, bare `grid` — never a specific one.
3. **`grid update`.** The explicit upgrade: a Linux self-contained binary downloads the new asset,
   verifies it against the release's SHA256SUMS and renames itself into place; a uv-installed wheel
   re-runs ``uv tool install --force`` on the release wheel (exactly what install.sh does on
   macOS). A running grid keeps the old binary until the operator restarts it — nothing here
   replaces a live process.

The version resolution deliberately goes through ``github.com`` (the ``/releases/latest`` redirect,
with the releases Atom feed as fallback), never ``api.github.com``: the unauthenticated API limit is
60 req/hr per IP, so an office behind one NAT address would have every notice 403 by lunchtime —
the same reason install.sh:56 does it this way.
"""
from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from shared import jsonio, paths
from shared._version import __version__

# How old the cached answer must be before another check is worth a spawn — and how long a shown
# notice stays suppressed. One line a day per machine is the whole budget; the check and the notice
# deliberately share the number so "checked today" and "notified today" cannot disagree.
CHECK_INTERVAL_S = 24 * 3600

# Kept short: this runs detached *beside* the user's command, so a hung network must never become
# a hung background process pool. The download in `grid update` is the operator's own call and
# uses its own, longer, timeout.
_CHECK_TIMEOUT_S = 5.0
_DOWNLOAD_TIMEOUT_S = 120.0

_USER_AGENT = "grid-cli-update"

_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


def _repo() -> tuple[str, str]:
    """(owner, repo) of the releases to track — the same env knobs install.sh honours."""
    return (
        os.environ.get("GRID_REPO_OWNER", "autonomous-ai"),
        os.environ.get("GRID_REPO_NAME", "autonomous-grid"),
    )


def _version_key(version: str) -> tuple[int, ...]:
    """Leading dotted numbers of a version as a comparable tuple (``0.3.41`` → ``(0, 3, 41)``).

    Prerelease/local suffixes are not compared: this CLI only ever ships plain ``X.Y.Z`` releases
    (packaging/build_binary.sh stamps them), so anything after the numeric run — ``+dev`` on a
    source checkout included — carries no ordering meaning here. Unequal-length tuples pad with
    zeros so ``0.4`` compares above ``0.3.9``.
    """
    match = _TAG_RE.match(version.strip())
    numbers = tuple(int(p) for p in match.group(1).split(".")) if match else ()
    return numbers


def is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` (a release tag or version) is strictly newer than ``current``."""
    cand, cur = _version_key(candidate), _version_key(current)
    if not cand or not cur:
        return False
    width = max(len(cand), len(cur))
    return cand + (0,) * (width - len(cand)) > cur + (0,) * (width - len(cur))


def _is_dev_build() -> bool:
    return "+dev" in __version__


def _is_binary_build() -> bool:
    """True inside the Nuitka-compiled self-contained binary (the Linux install shape).

    Nuitka injects ``__compiled__`` into builtins of every compiled module — the documented
    detector, and it cannot fire on a wheel/source run.
    """
    return hasattr(builtins, "__compiled__")


# --------------------------------------------------------------------------- cache


def _read_cache() -> dict[str, Any]:
    """The check cache, or ``{}`` when absent/corrupt/unreadable.

    Deliberately NOT ``jsonio.load_json``: that raises ``SystemExit`` on a corrupt file, and nothing
    in this module may end a user's command — a torn ``update-check.json`` must mean "no cached
    answer", not ``grid ls`` dying. The strict reader stays right for state the user acts on.
    """
    try:
        data = json.loads(paths.update_check_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache: dict[str, Any]) -> None:
    try:
        jsonio.atomic_write_json(paths.update_check_file(), cache)
    except OSError:
        pass  # a read-only GRID_HOME loses the cache, never the command


def _fresh(cache: dict[str, Any], now: float) -> bool:
    try:
        return now - float(cache.get("checked_at", 0.0)) < CHECK_INTERVAL_S
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------- version check


def fetch_latest_version(client: httpx.Client | None = None) -> str | None:
    """The newest release version (no leading ``v``), or ``None`` when it cannot be resolved.

    Redirect-of-``/releases/latest`` first, Atom feed second — install.sh:``latest_release_tag``
    ported to httpx, for the rate-limit reason in the module docstring. Never raises.
    """
    owner, repo = _repo()
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_CHECK_TIMEOUT_S, follow_redirects=False)
    try:
        try:
            resp = client.get(
                f"https://github.com/{owner}/{repo}/releases/latest",
                headers={"User-Agent": _USER_AGENT},
            )
            location = resp.headers.get("location", "")
            tag = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
            version = _parse_tag(tag)
            if version:
                return version
        except httpx.HTTPError:
            pass
        try:
            resp = client.get(
                f"https://github.com/{owner}/{repo}/releases.atom",
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            match = re.search(r"/releases/tag/([^\"<\s]+)", resp.text)
            if match:
                return _parse_tag(match.group(1))
        except httpx.HTTPError:
            pass
    finally:
        if own_client:
            client.close()
    return None


def _parse_tag(tag: str) -> str | None:
    """``v0.3.41`` → ``0.3.41``; anything that is not version-shaped (a page URL, an empty
    redirect) → ``None``, which makes the caller fall through to the next source."""
    match = _TAG_RE.match(tag.strip())
    if not match or tag.strip() != match.group(0):
        return None
    return match.group(1)


def run_check() -> int:
    """Resolve the newest version once and refresh the cache. Always succeeds — it runs detached
    behind ``grid __update-check`` and a failed check must only delay the next attempt."""
    cache = _read_cache()
    try:
        latest = fetch_latest_version()
    except Exception:
        latest = None
    # ``checked_at`` advances even when the resolution failed: without it every command would
    # re-spawn a check against an offline network until the network returns.
    cache["checked_at"] = time.time()
    cache["latest"] = latest
    _write_cache(cache)
    return 0


# ------------------------------------------------------------------ spawn + notice


def _stderr_is_tty() -> bool:
    try:
        return bool(sys.stderr) and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _opted_out() -> bool:
    return os.environ.get("GRID_NO_UPDATE_CHECK", "").strip().lower() not in ("", "0", "false")


def _notice_allowed(args: argparse.Namespace | None, json_requested: bool) -> bool:
    """Every suppression the notice (and the spawn behind it) honours, in one decision."""
    if _is_dev_build() or _opted_out():
        return False
    if json_requested or not _stderr_is_tty():
        return False
    command = getattr(args, "command", None) if args is not None else None
    if command == "update":  # mid-upgrade the notice would contradict the command running
        return False
    return True


def _self_command(*sub: str) -> list[str]:
    """ argv to re-exec this very build with an internal subcommand.

    The binary must re-exec *itself* (not a PATH lookup of some other grid): the check has to
    describe the build that is actually running. A bare ``sys.argv[0]`` can be relative (``./grid``)
    and a background process gets no second chance at cwd, so resolve before spawning.
    """
    if _is_binary_build():
        argv0 = sys.argv[0] or ""
        candidate = Path(argv0) if os.path.isabs(argv0) else None
        if candidate is None or not candidate.is_file():
            found = shutil.which(os.path.basename(argv0) or "grid") or shutil.which("grid")
            candidate = Path(found) if found else None
        if candidate is not None and candidate.is_file():
            return [str(candidate), *sub]
        # No resolvable path: better no check than a check of the wrong build.
        return []
    return [sys.executable, "-m", "cli", *sub]


def maybe_spawn_check(args: argparse.Namespace | None, *, json_requested: bool = False) -> None:
    """Spawn the detached background check when the cache is due. Never raises, never waits."""
    try:
        if not _notice_allowed(args, json_requested=json_requested):
            return
        if _fresh(_read_cache(), time.time()):
            return
        argv = _self_command("__update-check")
        if not argv:
            return
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell; detached so the user's command exits first
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def print_notice(args: argparse.Namespace | None, *, json_requested: bool = False) -> None:
    """The one-line stale-version notice on stderr, at most once per interval. Never raises.

    Reads only the cache — the spawn in ``maybe_spawn_check`` keeps it current, so this stays
    instant even with the network down. The first run after a release usually discovers and shows
    nothing (check still in flight); the next run shows it, which is the trade for zero latency.
    """
    try:
        if not _notice_allowed(args, json_requested=json_requested):
            return
        cache = _read_cache()
        latest = cache.get("latest")
        if not isinstance(latest, str) or not is_newer(latest, __version__):
            return
        now = time.time()
        try:
            notified_at = float(cache.get("notified_at", 0.0))
        except (TypeError, ValueError):
            notified_at = 0.0
        if now - notified_at < CHECK_INTERVAL_S:
            return
        print(
            f"\nA new version of grid is available: {__version__} → {latest}. "
            "Run `grid update` to upgrade.",
            file=sys.stderr,
        )
        cache["notified_at"] = now
        _write_cache(cache)
    except Exception:
        pass


# ------------------------------------------------------------------ `grid update`


def cmd_update(args: argparse.Namespace) -> int:
    """Upgrade the CLI in place — or say exactly why it cannot, with the manual path."""
    current = __version__
    if _is_dev_build():
        raise SystemExit(
            f"`grid update` can't upgrade a source checkout ({current}) — it would not know which "
            "release to install. `git pull` (or reinstall from the release wheel) instead."
        )
    latest = fetch_latest_version()
    if latest is None:
        raise SystemExit(
            "could not resolve the latest grid release from github.com — check your network, or "
            "install a known version with:  curl -fsSL https://grid.autonomous.ai/install.sh | "
            "GRID_VERSION=X.Y.Z bash"
        )
    if not is_newer(latest, current):
        print(f"grid {current} is the latest version.")
        return 0
    if getattr(args, "check", False):
        print(
            f"grid {current} → {latest} is available. Run `grid update` to install it."
        )
        return 0
    if _is_binary_build():
        _update_binary(latest)
    else:
        _update_wheel(latest)
    print(f"Updated grid {current} → {latest}.")
    print("Any grid already running keeps serving the old version until you restart it "
          "(`grid stop`, then `grid start`).")
    return 0  # both strategies either replaced the CLI or raised SystemExit; there is no partial win


def _arch_tag() -> str:
    import platform

    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    raise SystemExit(f"no grid binary is published for architecture {machine!r}")


def _os_tag() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    raise SystemExit(
        f"`grid update` does not know how to upgrade on {sys.platform!r} — reinstall with the "
        "command from the docs."
    )


def _release_base(version: str) -> str:
    owner, repo = _repo()
    return f"https://github.com/{owner}/{repo}/releases/download/v{version}"


def _fetch_bytes(url: str, client: httpx.Client | None = None) -> bytes | None:
    """GET a release asset whole, or ``None`` on any failure (404 is a normal answer here: the
    SHA256SUMS file is optional, exactly as install.sh treats it)."""
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True)
    try:
        resp = client.get(url, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError:
        return None
    finally:
        if own_client:
            client.close()


def _installed_binary_path() -> Path:
    """The on-disk binary to replace. Nuitka onefile exposes the launcher path; a bare relative
    ``argv[0]`` and a PATH lookup are the fallbacks, in that order of trustworthiness."""
    try:  # Nuitka onefile keeps the real launcher path here (standalone builds have no such module)
        import __nuitka.onefile  # type: ignore[import-not-found]

        path = __nuitka.onefile.getBinaryPath()
        if path and Path(path).is_file():
            return Path(path)
    except Exception:
        pass
    argv0 = sys.argv[0] or ""
    for candidate in (argv0 if os.path.isabs(argv0) else None, shutil.which("grid")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit(
        "could not locate the installed grid binary to replace — rerun the installer:  "
        "curl -fsSL https://grid.autonomous.ai/install.sh | bash"
    )


def _update_binary(latest: str) -> None:
    """Download the new release binary and rename it over this one (the install.sh sequence,
    in-process). ``os.replace`` is the whole point: the running process keeps its already-mapped
    inode, the ``agrid`` symlink (same directory, name ``grid``) keeps resolving, and there is no
    window where the path holds a partial file."""
    asset = f"grid-{_os_tag()}-{_arch_tag()}"
    base = _release_base(latest)
    payload = _fetch_bytes(f"{base}/{asset}")
    if payload is None:
        raise SystemExit(
            f"the {latest} release has no {asset} asset to download — if that is wrong, rerun the "
            "installer instead:  curl -fsSL https://grid.autonomous.ai/install.sh | bash"
        )
    # Hard-fail on a mismatch; only a release shipping no SHA256SUMS at all skips verification —
    # byte-for-byte install.sh's rule, so `grid update` and the installer cannot disagree about
    # what a good download is.
    sums = _fetch_bytes(f"{base}/SHA256SUMS")
    if sums is not None:
        want = None
        for line in sums.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset:
                want = parts[0].lower()
                break
        got = hashlib.sha256(payload).hexdigest()
        if want is not None and got != want:
            raise SystemExit(f"checksum mismatch for {asset}: got {got}, want {want}. Nothing installed.")

    target = _installed_binary_path()
    mode = target.stat().st_mode & 0o777
    tmp = target.parent / f".{target.name}.new-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SystemExit(
            f"could not replace {target}: {exc}\n"
            "If it belongs to another account, rerun the installer as that account."
        ) from None


def _update_wheel(latest: str) -> None:
    """The uv-tool shape (macOS's install path, and any wheel install): hand the release wheel
    back to ``uv tool install --force``, exactly as install.sh:``install_macos_wheel`` does."""
    wheel_url = f"{_release_base(latest)}/grid-{latest}-py3-none-any.whl"
    uv = shutil.which("uv") or (Path.home() / ".local" / "bin" / "uv")
    uv = str(uv)
    if not Path(uv).is_file():
        raise SystemExit(
            "this grid was not installed as a self-contained binary, and no `uv` was found to "
            "reinstall the wheel with — install uv (https://astral.sh/uv) or rerun the installer:  "
            "curl -fsSL https://grid.autonomous.ai/install.sh | bash"
        )
    try:
        result = subprocess.run([uv, "tool", "install", "--force", wheel_url])
    except OSError as exc:
        raise SystemExit(f"could not run `{uv} tool install`: {exc}") from None
    if result.returncode != 0:
        raise SystemExit(f"`uv tool install` failed for {wheel_url} (exit {result.returncode}).")
