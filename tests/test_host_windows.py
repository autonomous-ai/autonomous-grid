"""Host facts on Windows without psutil (`shared.system.host`).

psutil is an optional dependency, and Windows has neither ``os.sysconf`` nor ``os.statvfs``: a stock
install reported 0 GB of RAM (so the CPU budget fit no model at all), a 0 GB disk, and a CPU brand of
"Intel64 Family 6 Model 158…" that the bandwidth table cannot match. The Windows branches run here on
any OS by flipping the module's platform flag; the real kernel call runs on the Windows CI job.
"""
from __future__ import annotations

import sys
from collections import namedtuple

import pytest

from shared.system import host

GIB = 1024 ** 3


@pytest.fixture
def windows_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # `import psutil` raises ImportError
    monkeypatch.setattr(host, "_IS_WINDOWS", True)


def test_windows_memory_snapshot_uses_global_memory_status(windows_without_psutil, monkeypatch):
    monkeypatch.setattr(host, "_win_memory_status", lambda: (16 * GIB, 9 * GIB, 44.0))
    assert host._memory_snapshot() == (16 * GIB, 9 * GIB, 44.0)


def test_windows_memory_used_mb_is_total_minus_available(windows_without_psutil, monkeypatch):
    monkeypatch.setattr(host, "_win_memory_status", lambda: (16 * GIB, 9 * GIB, 44.0))
    assert host.memory_used_mb() == 7 * 1024.0


def test_windows_memory_unreadable_still_says_unknown(windows_without_psutil, monkeypatch):
    monkeypatch.setattr(host, "_win_memory_status", lambda: None)
    assert host.memory_used_mb() is None


def test_windows_disk_snapshot_uses_disk_usage(windows_without_psutil, monkeypatch, tmp_path):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(host.shutil, "disk_usage", lambda path: Usage(500 * GIB, 200 * GIB, 300 * GIB))
    monkeypatch.delattr(host.os, "statvfs", raising=False)
    assert host.disk_gb(str(tmp_path)) == (500.0, 200.0)


def test_windows_cpu_brand_reads_the_registry(windows_without_psutil, monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Windows")
    monkeypatch.setattr(host, "_win_cpu_name", lambda: "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz")
    assert host.cpu_brand() == "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz"


def test_windows_cpu_brand_falls_back_when_the_registry_fails(windows_without_psutil, monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Windows")
    monkeypatch.setattr(host, "_win_cpu_name", lambda: None)
    monkeypatch.setattr(host.platform, "processor", lambda: "Intel64 Family 6 Model 158 Stepping 10")
    assert host.cpu_brand() == "Intel64 Family 6 Model 158 Stepping 10"


def test_posix_memory_snapshot_never_asks_windows(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(host, "_IS_WINDOWS", False)

    def windows_only():
        raise AssertionError("POSIX must not reach the Windows probe")

    monkeypatch.setattr(host, "_win_memory_status", windows_only)
    host._memory_snapshot()


@pytest.mark.skipif(sys.platform != "win32", reason="the real GlobalMemoryStatusEx call")
def test_real_global_memory_status_reports_a_total():
    total, available, load = host._win_memory_status()
    assert total > 0
    assert 0 <= available <= total
    assert 0.0 <= load <= 100.0


@pytest.mark.skipif(sys.platform != "win32", reason="the real registry read")
def test_real_cpu_name_is_a_brand_string():
    name = host._win_cpu_name()
    assert name and "Family" not in name
