"""The training extra comes from this repository's release wheel, never from PyPI.

PyPI's `grid` is an unrelated package with no `train` extra, so `pip install 'grid[train]'` installs
the wrong thing; and a `uv tool` install, the installer's wheel path, has no `pip` in it at all. So
every missing-dependency message prints one command, built in one place (`shared.release`).
Measured 2026-10-25 on macOS arm64 with uv 0.11.12: the command installs torch, transformers, trl,
peft, datasets, verifiers and mlx-lm into the tool environment, and a plain `uv tool install
--force <wheel>` (what `grid update` ran) drops them again.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import pytest

from shared import release

ROOT = Path(__file__).resolve().parents[1]
WHEEL = "https://github.com/autonomous-ai/autonomous-grid/releases/download/v1.2.3/grid-1.2.3-py3-none-any.whl"


@pytest.fixture
def uv_on_path(monkeypatch):
    monkeypatch.setattr(release.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_the_command_installs_this_versions_release_wheel_with_the_extra(uv_on_path):
    assert release.train_install_command("1.2.3") == f'uv tool install --force "grid[train] @ {WHEEL}"'


def test_without_uv_the_command_installs_uv_first(monkeypatch):
    """A Linux binary install usually has no uv: install.sh bootstraps it only on the wheel path."""
    monkeypatch.setattr(release.shutil, "which", lambda name: None)
    assert release.train_install_command("1.2.3") == (
        f'{release.UV_INSTALLER} && uv tool install --force "grid[train] @ {WHEEL}"'
    )


def test_a_source_checkout_installs_the_extra_into_itself(uv_on_path):
    """`0.0.0+dev` has no release wheel to point at."""
    assert release.train_install_command("0.0.0+dev").startswith("uv pip install -e '.[train]'")


def test_no_tracked_file_sends_anyone_to_pypi_for_the_extra():
    # `--others` too: a new file is not in the index until it is committed, and a guard over the index
    # alone passes before the commit and fails after it.
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split("\0")
    pypi = re.compile(r"pip install\s+['\"]?grid\[")
    hits = [
        f"{name}:{number}"
        for name in tracked
        if name.endswith((".py", ".md", ".sh", ".html", ".js", ".toml")) and name != Path(__file__).relative_to(ROOT).as_posix()
        and (ROOT / name).is_file()
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if pypi.search(line)
    ]
    assert hits == []
    assert pypi.search("pip install 'grid[train]'")  # positive control for the pattern


def test_the_mac_guide_shows_the_same_command(uv_on_path):
    """The guide fills the version in from `grid --version`, so the line is pasted, not edited."""
    guide = (ROOT / "docs" / "start-on-a-mac.md").read_text(encoding="utf-8")
    assert "v=$(grid --version | awk '{print $2}')" in guide
    assert release.train_install_command("$v") in guide


def _doctor_output(monkeypatch, capsys, deps):
    import train.config
    import train.run
    from cli import train as cli_train

    monkeypatch.setattr(train.config, "load_config", lambda path: None)
    monkeypatch.setattr(train.run, "doctor", lambda cfg: {
        "deps": deps,
        "endpoint": {"ok": False, "model": "m", "detail": "unreachable"},
        "data": {"ok": False, "detail": "no examples yet"},
        "trainer": {"ok": False, "detail": "torch is not installed"},
    })
    cli_train.cmd_train_doctor(argparse.Namespace(config=None, json=False))
    return capsys.readouterr().out


CORE = ("torch", "transformers", "trl", "peft", "datasets")


def test_doctor_prints_the_command_for_a_missing_dependency(monkeypatch, capsys):
    out = _doctor_output(monkeypatch, capsys, {**dict.fromkeys(CORE), "verifiers": None})
    assert out.count(release.train_install_command()) == 1  # once, under Dependencies, not per line


def test_doctor_prints_no_command_when_only_an_optional_dependency_is_missing(monkeypatch, capsys):
    out = _doctor_output(monkeypatch, capsys, {**dict.fromkeys(CORE, "1.0"), "verifiers": None})
    assert release.train_install_command() not in out


def test_the_extra_is_detected_by_trl(monkeypatch):
    from cli import update

    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: object() if name == "trl" else None)
    assert update._has_train_extra() is True
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: None)
    assert update._has_train_extra() is False


def test_grid_update_keeps_the_training_extra(monkeypatch, tmp_path):
    """Without this, `grid update` reinstalls the plain wheel and the trainer silently goes."""
    from cli import update

    uv = tmp_path / "uv"
    uv.write_text("")
    monkeypatch.setattr(update.shutil, "which", lambda name: str(uv))
    ran: list[list[str]] = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv: ran.append(argv) or subprocess.CompletedProcess(argv, 0))

    monkeypatch.setattr(update, "_has_train_extra", lambda: True)
    update._update_wheel("1.2.3")
    monkeypatch.setattr(update, "_has_train_extra", lambda: False)
    update._update_wheel("1.2.3")

    assert ran == [
        [str(uv), "tool", "install", "--force", f"grid[train] @ {WHEEL}"],
        [str(uv), "tool", "install", "--force", WHEEL],
    ]
