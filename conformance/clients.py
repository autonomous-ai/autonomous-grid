"""The clients in the field, installed the way their users installed them (grid-platform ticket 13).

* Every released public `grid` from 0.3.47 on, from its RELEASE WHEEL on GitHub Releases. Not a checkout: a checkout
  is what we have now, and the question is what the fleet runs.
* The harness's pinned `grid`, read from the manifest the harness daemon itself follows.

Each version gets a venv in a cache (`GRID_CONFORMANCE_CACHE`, default `~/.cache/grid-conformance`) and each run gets
its own `GRID_HOME`, holding the credentials a signed-in user would have for the grid under test.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

#: Every release still in the field. 0.3.47 is what the harness pinned until 2026-09-24; nothing older is supported.
RELEASED = ("0.3.47", "0.3.48", "0.3.49", "0.3.50", "0.3.51", "0.3.52")
WHEEL_URL = "https://github.com/autonomous-ai/autonomous-grid/releases/download/v{v}/grid-{v}-py3-none-any.whl"
HARNESS_MANIFEST = "https://storage.googleapis.com/s3-autonomous-upgrade-3/harness/runtime/grid/metadata.json"
#: `--no-wake` (grid-reads-without-waking issue 01) first shipped in 0.3.49; older releases refuse it at argparse.
NO_WAKE_SINCE = (0, 3, 49)


def parse(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def harness_pin() -> str:
    """The `grid` the harness daemon installs: `GRID_HARNESS_PIN`, else its live manifest's entry for this platform."""
    if os.environ.get("GRID_HARNESS_PIN"):
        return os.environ["GRID_HARNESS_PIN"]
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    key = f"{'darwin' if platform.system() == 'Darwin' else 'linux'}-{machine}"
    return httpx.get(HARNESS_MANIFEST, timeout=20).json()["grid"][key]["version"]


def _cache() -> Path:
    return Path(os.environ.get("GRID_CONFORMANCE_CACHE") or Path.home() / ".cache" / "grid-conformance")


def install(version: str) -> Path:
    """The `grid` executable of `version`, installed from its release wheel into a cached venv."""
    venv = _cache() / f"grid-{version}"
    exe = venv / "bin" / "grid"
    if exe.exists():
        return exe
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the released clients")
    tmp = venv.with_name(venv.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run([uv, "venv", "-q", "--python", "3.12", str(tmp)], check=True)
    subprocess.run([uv, "pip", "install", "-q", "--python", str(tmp / "bin" / "python"), WHEEL_URL.format(v=version)],
                   check=True)
    shutil.rmtree(venv, ignore_errors=True)
    tmp.rename(venv)
    _repoint_scripts(venv, tmp)
    return exe


def _repoint_scripts(venv: Path, old: Path) -> None:
    """A venv's scripts name its interpreter by absolute path; the rename moved it."""
    for script in (venv / "bin").iterdir():
        if script.is_file() and not script.is_symlink():
            try:
                text = script.read_text()
            except UnicodeDecodeError:
                continue
            if str(old) in text:
                script.write_text(text.replace(str(old), str(venv)))


@dataclass(frozen=True)
class Client:
    version: str
    exe: Path
    home: Path

    @property
    def has_no_wake(self) -> bool:
        return parse(self.version) >= NO_WAKE_SINCE

    def run(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.exe.parent}:{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "GRID_HOME": str(self.home / ".grid"),
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "GRID_NO_UPDATE_CHECK": "1",
        }
        return subprocess.run([str(self.exe), *args], env=env, capture_output=True, text=True, timeout=timeout,
                              check=False)

    def sign_in(self, stack, *, token: str, roles: list[str], scopes: list[str], name: str = "conformance") -> None:
        """The credentials `grid login` + `grid network join` leave behind, for the grid under test."""
        import tomli_w

        grid_home = self.home / ".grid"
        grid_home.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        credentials = {
            "api_url": stack.control_plane_url,
            "session_token": "conformance-session",
            "user": {"google_sub": "conformance", "email": "conformance@conformance.invalid", "name": "conformance",
                     "created_at": now, "last_seen_at": now},
            "networks": [{
                "network_id": stack.network_id, "name": name, "network_type": "permissionless",
                "lan_signaling_url": stack.grid_url, "access_token": token, "refresh_token": "conformance-refresh",
                "email": "conformance@conformance.invalid", "roles": roles, "scopes": scopes,
                "device_id": "conformance-device", "node_id": f"node-{self.version.replace('.', '-')}",
                "member_epoch": 1, "network_epoch": 1, "expires_at": 4_102_444_800, "refresh_expires_at": 4_102_444_800,
            }],
        }
        (grid_home / "credentials.toml").write_text(tomli_w.dumps(credentials))
        (grid_home / "credentials.toml").chmod(0o600)


def envelope(proc: subprocess.CompletedProcess) -> dict | None:
    """The `--json` error envelope a failed command wrote: ONE JSON line on stderr (`grid-protocol` `cli-error`), which a
    human-readable line may follow. None when there is none."""
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            if isinstance(answer, dict) and "error" in answer:
                return answer
    return None
