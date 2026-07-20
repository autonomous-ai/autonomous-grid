"""The Mac's real CPU architecture — the hardware's, not this process's."""

from __future__ import annotations

import platform
import subprocess


def native_machine() -> str:
    """The architecture of the machine, even when Grid itself runs under Rosetta.

    `platform.machine()` reports the *process* architecture: an x86_64 Python on an M-series Mac
    (which is what an Intel Homebrew's `uv` produces) reports `x86_64`. Installers must follow the
    hardware instead — otherwise Grid downloads an Intel llama.cpp and an Intel Python onto Apple
    Silicon, and from then on every check downstream believes this is an Intel Mac.
    """
    if platform.system() != "Darwin":
        return platform.machine()
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return platform.machine()
    return "arm64" if result.stdout.strip() == "1" else platform.machine()


def normalized_machine() -> str:
    """The hardware architecture as a release-artifact tag: ``x86_64`` or ``aarch64``.

    Vendors name their downloads with these two spellings, but the OS reports the CPU under many
    aliases — Windows says ``AMD64``/``ARM64``, macOS says ``arm64``, Linux says ``x86_64``. Fold
    them all so installers pick the right build regardless of platform.
    """
    machine = native_machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return machine
