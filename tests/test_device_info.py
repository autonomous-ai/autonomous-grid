"""Tests for `collect_device_info()`.

The device-info object is a stable, serializable description of the machine, so its
shape is a contract: these tests pin the field names/types and the consistency
invariant between device_class, backend and each GPU's backend, so a rename or a
mismatched backend is caught here. Values are machine-dependent — we assert types
and relationships, not exact numbers.
"""
from __future__ import annotations

from shared.system import apple, bandwidth, device, device_info, gpu
from shared.system.device import Budget


# field -> accepted python type(s); `None` in a tuple means the field is nullable.
_TOP = {
    "device_class": (str,),
    "backend": (str,),
    "usable_bytes": (int,),
    "mem_bandwidth_gbps": (int, float, None),
    "compute_gflops": (int, float, None),
    "detected": (str,),
    "machine": (dict,),
    "cpu": (dict,),
    "memory": (dict,),
    "disk": (dict,),
    "gpus": (list,),
}
_MACHINE = {
    "model": (str, None),
    "platform": (str,),
    "os_name": (str,),
    "os_version": (str,),
    "arch": (str,),
}
_CPU = {
    "brand": (str,),
    "physical_cores": (int,),
    "logical_threads": (int,),
}
_MEMORY = {"total_gb": (int, float), "available_gb": (int, float), "used_percent": (int, float)}
_DISK = {"total_gb": (int, float), "free_gb": (int, float)}
_GPU = {
    "index": (int,),
    "name": (str,),
    "backend": (str,),
    "memory_total_mb": (int, float),
    "memory_used_mb": (int, float),
    "compute_cap": (str, None),
    "driver_version": (str, None),
    "utilization_pct": (int, float),
    "core_count": (int, None),
}


def _check_block(obj, schema, where):
    assert isinstance(obj, dict), f"{where} must be a dict"
    assert set(obj.keys()) == set(schema.keys()), (
        f"{where} keys {sorted(obj.keys())} != {sorted(schema.keys())}"
    )
    for field, types in schema.items():
        allow_none = None in types
        py_types = tuple(t for t in types if t is not None)
        val = obj[field]
        if val is None:
            assert allow_none, f"{where}.{field} may not be None"
            continue
        # bool is an int subclass — never accept it where a real int/number is wanted.
        assert not isinstance(val, bool), f"{where}.{field} must not be a bool"
        assert isinstance(val, py_types), (
            f"{where}.{field}={val!r} is {type(val).__name__}, want {py_types}"
        )


def _assert_shape(info):
    _check_block(info, _TOP, "device_info")
    _check_block(info["machine"], _MACHINE, "machine")
    _check_block(info["cpu"], _CPU, "cpu")
    _check_block(info["memory"], _MEMORY, "memory")
    _check_block(info["disk"], _DISK, "disk")
    for i, g in enumerate(info["gpus"]):
        _check_block(g, _GPU, f"gpus[{i}]")
    assert info["device_class"] in ("apple-silicon", "nvidia", "cpu")
    assert info["backend"] in ("metal", "cuda", "cpu")


# The real-machine output must always carry every field with the right type.
def test_collect_device_info_matches_contract_shape():
    info = device_info.collect_device_info()
    _assert_shape(info)


# Physical cores can never exceed logical threads.
def test_physical_cores_within_logical_threads():
    info = device_info.collect_device_info()
    phys = info["cpu"]["physical_cores"]
    logical = info["cpu"]["logical_threads"]
    assert isinstance(phys, int) and phys >= 1
    assert isinstance(logical, int) and logical >= 1
    assert phys <= logical


# Probes are best-effort: a total probe failure still yields a well-shaped object.
def test_collect_never_raises_when_probes_fail(monkeypatch):
    """Every subprocess/psutil probe is best-effort — a total probe failure must
    still yield a well-shaped object, never an exception."""
    def boom(*a, **k):
        raise OSError("probe exploded")

    monkeypatch.setattr(apple, "_run", boom)
    monkeypatch.setattr(apple, "describe_chip", lambda: ("", ""))
    monkeypatch.setattr(apple, "gpu_core_count", lambda: None)
    # Even the budget resolver failing must not crash collection.
    info = device_info.collect_device_info()
    _assert_shape(info)


def test_gpu_core_count_never_raises(monkeypatch):
    # Patch the real subprocess boundary: _run must swallow this and return "",
    # so gpu_core_count degrades to None instead of raising.
    def boom(*a, **k):
        raise OSError("no system_profiler")

    monkeypatch.setattr(apple.subprocess, "check_output", boom)
    assert apple._run(["anything"]) == ""
    assert apple.gpu_core_count() is None  # best-effort → None, not an exception
    assert apple.describe_chip() == ("", "")  # chip probe likewise degrades cleanly


# Apple Silicon: chip/model come from system_profiler, GPU is synthesised.
def _force_apple(monkeypatch, *, chip="Apple M3 Max", model="MacBook Pro (Mac15,9)",
                 cores=40, total_mb=65536.0):
    monkeypatch.setattr(
        device, "resolve_budget",
        lambda: Budget(int(60 * 1024 ** 3), "vram",
                       "Apple Silicon, 60 GB usable of 64 GB unified memory", backend="metal"),
    )
    monkeypatch.setattr(apple, "describe_chip", lambda: (model, chip))
    monkeypatch.setattr(apple, "gpu_core_count", lambda: cores)
    monkeypatch.setattr(gpu, "load_snapshot",
                        lambda **k: {"memory_total_mb": total_mb, "memory_used_mb": 4096.0,
                                     "gpu_util": 5.0})
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [])


def test_apple_silicon_fields(monkeypatch):
    _force_apple(monkeypatch)
    info = device_info.collect_device_info()
    _assert_shape(info)
    assert info["device_class"] == "apple-silicon"
    assert info["backend"] == "metal"
    assert info["cpu"]["brand"]                       # non-empty
    assert info["machine"]["model"]                   # non-empty on Apple
    assert len(info["gpus"]) == 1
    g = info["gpus"][0]
    assert g["backend"] == "metal"
    assert g["core_count"] == 40
    assert g["compute_cap"] is None and g["driver_version"] is None
    assert device_info.consistency_ok(info)


def test_apple_silicon_compute_gflops_scales_with_core_count(monkeypatch):
    # Read the per-core figure from the table rather than repeating it: the rate is
    # per generation and gets revised, and a copy here would silently go stale.
    per_core = bandwidth._APPLE_GFLOPS_PER_CORE["m3"]        # _force_apple pins an M3 Max
    _force_apple(monkeypatch, cores=40)
    assert device_info.collect_device_info()["compute_gflops"] == 40 * per_core
    # Same chip, half the GPU cores -> half the compute. That proportionality is the
    # whole point of counting cores instead of using one number per backend.
    _force_apple(monkeypatch, cores=20)
    assert device_info.collect_device_info()["compute_gflops"] == 20 * per_core


def test_cpu_only_device_reports_compute_gflops_from_physical_cores():
    info = device_info.collect_device_info()
    if info["backend"] == "cpu":
        assert info["compute_gflops"] == info["cpu"]["physical_cores"] * 190.0


def test_apple_silicon_core_count_null_is_tolerated(monkeypatch):
    _force_apple(monkeypatch, cores=None)
    info = device_info.collect_device_info()
    assert info["gpus"][0]["core_count"] is None      # int-or-null, never raises
    assert device_info.consistency_ok(info)


# NVIDIA cards report compute_cap/driver but no Apple-style core count.
def test_nvidia_gpu_has_compute_cap_and_null_core_count(monkeypatch):
    fake = gpu.GpuInfo(index=0, name="RTX 4090", driver_version="550.00",
                       compute_cap="8.9", memory_total_mb=24576.0,
                       memory_used_mb=2048.0, utilization_pct=12.0)
    monkeypatch.setattr(
        device, "resolve_budget",
        lambda: Budget(int(22 * 1024 ** 3), "vram", "NVIDIA RTX 4090, 22 GB VRAM", backend="cuda"),
    )
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [fake])
    info = device_info.collect_device_info()
    _assert_shape(info)
    assert info["device_class"] == "nvidia"
    assert info["backend"] == "cuda"
    assert len(info["gpus"]) == 1
    g = info["gpus"][0]
    assert g["backend"] == "cuda"
    assert g["compute_cap"] == "8.9"
    assert g["driver_version"] == "550.00"
    assert g["core_count"] is None
    assert device_info.consistency_ok(info)


# device_class, backend and every GPU backend must agree.
def test_consistency_invariant_on_real_machine():
    info = device_info.collect_device_info()
    assert device_info.consistency_ok(info)


def test_consistency_validator_rejects_mismatch():
    # device_class says apple-silicon but backend says cuda — must be rejected.
    bad = {"device_class": "apple-silicon", "backend": "cuda", "gpus": []}
    assert not device_info.consistency_ok(bad)
    # backend/class agree, but a gpu carries the wrong backend.
    bad2 = {"device_class": "nvidia", "backend": "cuda",
            "gpus": [{"backend": "metal"}]}
    assert not device_info.consistency_ok(bad2)
    # cpu class must carry no discrete gpus.
    bad3 = {"device_class": "cpu", "backend": "cpu",
            "gpus": [{"backend": "cuda"}]}
    assert not device_info.consistency_ok(bad3)


def test_consistency_validator_accepts_good_fixtures():
    good_apple = {"device_class": "apple-silicon", "backend": "metal",
                  "gpus": [{"backend": "metal"}]}
    good_nv = {"device_class": "nvidia", "backend": "cuda",
               "gpus": [{"backend": "cuda"}, {"backend": "cuda"}]}
    good_cpu = {"device_class": "cpu", "backend": "cpu", "gpus": []}
    assert device_info.consistency_ok(good_apple)
    assert device_info.consistency_ok(good_nv)
    assert device_info.consistency_ok(good_cpu)
