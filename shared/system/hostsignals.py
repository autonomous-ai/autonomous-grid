"""Best-effort host-safety telemetry for Grid's local allocator.

The collector deliberately has no dependency on the provider process.  Every probe is isolated:
one unsupported API, malformed command response, or missing sensor leaves that field unknown rather
than preventing the node from reporting the rest of its state.  Policy decisions about unknown
values belong in :mod:`shared.allocator.local`, not in this module.
"""

from __future__ import annotations

import ctypes
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from shared.system import gpu, host

HOST_SIGNALS_SCHEMA_VERSION = 1


class ThermalState(StrEnum):
    """Coarse, cross-platform thermal pressure reported by the host."""

    UNKNOWN = "unknown"
    NOMINAL = "nominal"
    FAIR = "fair"
    SERIOUS = "serious"
    CRITICAL = "critical"


def _finite_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _thermal_state(value: object) -> ThermalState:
    try:
        return ThermalState(str(value).lower())
    except ValueError:
        return ThermalState.UNKNOWN


@dataclass(frozen=True, slots=True)
class HostSignals:
    """One immutable host telemetry snapshot.

    Utilization and pressure fields are percentages in ``[0, 100]``.  ``None`` means the signal
    was not obtainable; zero is a real measurement and is never used as an unknown sentinel.
    ``timestamp`` is Unix time so snapshots can cross processes and survive restarts.
    """

    timestamp: float
    battery_percent: float | None = None
    on_battery: bool | None = None
    battery_charging: bool | None = None
    idle_seconds: float | None = None
    user_active: bool | None = None
    thermal_state: ThermalState = ThermalState.UNKNOWN
    temperature_celsius: float | None = None
    gpu_utilization_percent: float | None = None
    gpu_memory_percent: float | None = None
    cpu_utilization_percent: float | None = None
    load_average_1m: float | None = None
    load_per_cpu: float | None = None
    memory_percent: float | None = None
    network_available: bool | None = None
    collector_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Normalize the enum/tuple without rejecting direct construction containing bad telemetry.
        # The evaluator treats malformed values exactly like unavailable values, which is important
        # at a process boundary where a bad sensor must not crash or falsely quarantine a host.
        if not isinstance(self.thermal_state, ThermalState):
            object.__setattr__(self, "thermal_state", _thermal_state(self.thermal_state))
        raw_errors = (
            (self.collector_errors,)
            if isinstance(self.collector_errors, str)
            else self.collector_errors
        )
        object.__setattr__(
            self,
            "collector_errors",
            tuple(dict.fromkeys(str(item) for item in raw_errors if str(item))),
        )

    # Concise aliases make the units explicit at serialization boundaries while keeping common
    # policy terminology convenient for callers.
    @property
    def gpu_utilization(self) -> float | None:
        return self.gpu_utilization_percent

    @property
    def gpu_memory_pressure(self) -> float | None:
        return self.gpu_memory_percent

    @property
    def cpu_utilization(self) -> float | None:
        return self.cpu_utilization_percent

    @property
    def load_average(self) -> float | None:
        return self.load_average_1m

    @property
    def memory_pressure(self) -> float | None:
        return self.memory_percent

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = HOST_SIGNALS_SCHEMA_VERSION
        data["thermal_state"] = self.thermal_state.value
        data["collector_errors"] = list(self.collector_errors)
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HostSignals:
        """Decode an untrusted wire snapshot, degrading malformed readings to unknown."""

        try:
            version = int(value.get("schema_version", HOST_SIGNALS_SCHEMA_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid host-signals schema version") from exc
        if version != HOST_SIGNALS_SCHEMA_VERSION:
            raise ValueError("unsupported host-signals schema")

        invalid: list[str] = []

        def number(
            name: str,
            *aliases: str,
            minimum: float | None = None,
            maximum: float | None = None,
        ) -> float | None:
            raw: object = None
            present_name = name
            for candidate in (name, *aliases):
                if candidate in value:
                    raw = value[candidate]
                    present_name = candidate
                    break
            result = _finite_number(raw, minimum=minimum, maximum=maximum)
            if raw is not None and result is None:
                invalid.append(present_name)
            return result

        def boolean(name: str) -> bool | None:
            raw = value.get(name)
            result = _optional_bool(raw)
            if raw is not None and result is None:
                invalid.append(name)
            return result

        raw_thermal = value.get("thermal_state", ThermalState.UNKNOWN)
        thermal = _thermal_state(raw_thermal)
        if (
            raw_thermal not in (None, "")
            and thermal == ThermalState.UNKNOWN
            and str(raw_thermal).lower() != ThermalState.UNKNOWN
        ):
            invalid.append("thermal_state")

        raw_errors = value.get("collector_errors") or ()
        if isinstance(raw_errors, str) or not isinstance(raw_errors, (list, tuple)):
            raw_errors = ("malformed:collector_errors",)

        timestamp = number("timestamp", "observed_at", minimum=0.0)
        if timestamp is None:
            # A deterministic zero keeps the object serializable.  The evaluator marks the clock
            # malformed and will clamp it against prior state, so this cannot skip a cooldown.
            timestamp = 0.0

        errors = tuple(str(item) for item in raw_errors if str(item))
        errors += tuple(f"malformed:{name}" for name in invalid)
        return cls(
            timestamp=timestamp,
            battery_percent=number("battery_percent", minimum=0.0, maximum=100.0),
            on_battery=boolean("on_battery"),
            battery_charging=boolean("battery_charging"),
            idle_seconds=number("idle_seconds", minimum=0.0),
            user_active=boolean("user_active"),
            thermal_state=thermal,
            temperature_celsius=number(
                "temperature_celsius", "temperature_c", minimum=-100.0, maximum=250.0
            ),
            gpu_utilization_percent=number(
                "gpu_utilization_percent", "gpu_utilization", minimum=0.0, maximum=100.0
            ),
            gpu_memory_percent=number(
                "gpu_memory_percent", "gpu_memory_pressure", minimum=0.0, maximum=100.0
            ),
            cpu_utilization_percent=number(
                "cpu_utilization_percent", "cpu_utilization", minimum=0.0, maximum=100.0
            ),
            load_average_1m=number("load_average_1m", "load_average", minimum=0.0),
            load_per_cpu=number("load_per_cpu", minimum=0.0),
            memory_percent=number(
                "memory_percent", "memory_pressure", minimum=0.0, maximum=100.0
            ),
            network_available=boolean("network_available"),
            collector_errors=errors,
        )


def _run(command: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _linux_battery() -> tuple[float | None, bool | None, bool | None]:
    root = Path("/sys/class/power_supply")
    try:
        supplies = tuple(root.iterdir())
    except OSError:
        return None, None, None
    batteries = [item for item in supplies if _read_text(item / "type").lower() == "battery"]
    if not batteries:
        # A desktop without a battery is known not to be on battery power.
        return None, False, None
    capacities = [
        value
        for item in batteries
        if (value := _finite_number(_read_text(item / "capacity"), minimum=0, maximum=100))
        is not None
    ]
    statuses = {_read_text(item / "status").lower() for item in batteries}
    charging = any(status in {"charging", "full"} for status in statuses)
    discharging = any(status == "discharging" for status in statuses)
    percent = sum(capacities) / len(capacities) if capacities else None
    return percent, discharging, charging


def _macos_battery(timeout: float) -> tuple[float | None, bool | None, bool | None]:
    output = _run(["pmset", "-g", "batt"], timeout)
    if not output:
        return None, None, None
    match = re.search(r"(\d{1,3})%", output)
    percent = _finite_number(match.group(1), minimum=0, maximum=100) if match else None
    lower = output.lower()
    on_battery = False if "ac power" in lower else True if "battery power" in lower else None
    charging = False if "not charging" in lower or "discharging" in lower else None
    if any(word in lower for word in ("; charging", "; charged", "finishing charge")):
        charging = True
    return percent, on_battery, charging


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ac_line_status", ctypes.c_ubyte),
        ("battery_flag", ctypes.c_ubyte),
        ("battery_life_percent", ctypes.c_ubyte),
        ("system_status_flag", ctypes.c_ubyte),
        ("battery_life_time", ctypes.c_uint32),
        ("battery_full_life_time", ctypes.c_uint32),
    ]


def _windows_battery() -> tuple[float | None, bool | None, bool | None]:
    try:
        status = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None, None, None
    except (AttributeError, OSError):
        return None, None, None
    percent = None if status.battery_life_percent == 255 else float(status.battery_life_percent)
    on_battery = None if status.ac_line_status == 255 else status.ac_line_status == 0
    charging = None if status.battery_flag == 255 else bool(status.battery_flag & 8)
    return percent, on_battery, charging


def _battery_snapshot(timeout: float) -> tuple[float | None, bool | None, bool | None]:
    system = platform.system()
    if system == "Linux":
        return _linux_battery()
    if system == "Darwin":
        return _macos_battery(timeout)
    if system == "Windows":
        return _windows_battery()
    return None, None, None


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cb_size", ctypes.c_uint), ("dw_time", ctypes.c_uint32)]


def _windows_idle_seconds() -> float | None:
    try:
        info = _LastInputInfo()
        info.cb_size = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return None
        current = ctypes.windll.kernel32.GetTickCount()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    # DWORD tick counts wrap after roughly 49.7 days; unsigned subtraction handles it.
    elapsed_ms = (int(current) - int(info.dw_time)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def _idle_seconds(timeout: float) -> float | None:
    system = platform.system()
    if system == "Windows":
        return _windows_idle_seconds()
    if system == "Darwin":
        output = _run(["ioreg", "-c", "IOHIDSystem"], timeout)
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', output)
        return int(match.group(1)) / 1_000_000_000.0 if match else None
    if system == "Linux" and shutil.which("xprintidle"):
        value = _finite_number(_run(["xprintidle"], timeout), minimum=0.0)
        return value / 1000.0 if value is not None else None
    return None


def _linux_temperature() -> float | None:
    temperatures: list[float] = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        raw = _finite_number(_read_text(path))
        if raw is None:
            continue
        celsius = raw / 1000.0 if raw > 250.0 else raw
        if -100.0 <= celsius <= 250.0:
            temperatures.append(celsius)
    return max(temperatures) if temperatures else None


def _thermal_from_temperature(temperature: float | None) -> ThermalState:
    if temperature is None:
        return ThermalState.UNKNOWN
    if temperature >= 95.0:
        return ThermalState.CRITICAL
    if temperature >= 85.0:
        return ThermalState.SERIOUS
    if temperature >= 70.0:
        return ThermalState.FAIR
    return ThermalState.NOMINAL


def _thermal_snapshot(timeout: float) -> tuple[ThermalState, float | None]:
    system = platform.system()
    if system == "Linux":
        temperature = _linux_temperature()
        return _thermal_from_temperature(temperature), temperature
    if system == "Darwin":
        raw = _run(["sysctl", "-n", "kern.thermal_pressure"], timeout)
        value = _finite_number(raw, minimum=0)
        if value is None:
            return ThermalState.UNKNOWN, None
        states = {
            0: ThermalState.NOMINAL,
            1: ThermalState.FAIR,
            2: ThermalState.SERIOUS,
            3: ThermalState.CRITICAL,
        }
        return states.get(min(int(value), 3), ThermalState.UNKNOWN), None
    return ThermalState.UNKNOWN, None


def _cpu_ticks() -> tuple[int, int] | None:
    """Return ``(idle, total)`` monotonically increasing CPU ticks where supported."""

    if platform.system() != "Linux":
        return None
    line = _read_text(Path("/proc/stat")).splitlines()
    if not line or not line[0].startswith("cpu "):
        return None
    try:
        values = [int(item) for item in line[0].split()[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _network_available(timeout: float) -> bool | None:
    system = platform.system()
    if system == "Linux":
        ipv4_lines = _read_text(Path("/proc/net/route")).splitlines()
        ipv6_lines = _read_text(Path("/proc/net/ipv6_route")).splitlines()
        if not ipv4_lines and not ipv6_lines:
            return None
        for route in (line.split() for line in ipv4_lines[1:]):
            if len(route) >= 4 and route[1] == "00000000":
                try:
                    is_up = bool(int(route[3], 16) & 0x1)
                except ValueError:
                    continue
                carrier = _read_text(Path("/sys/class/net") / route[0] / "carrier")
                if is_up and carrier != "0":
                    return True
        for route in (line.split() for line in ipv6_lines):
            if (
                len(route) >= 10
                and route[0] == "0" * 32
                and route[1] == "00"
            ):
                try:
                    is_up = bool(int(route[8], 16) & 0x1)
                except ValueError:
                    continue
                carrier = _read_text(Path("/sys/class/net") / route[9] / "carrier")
                if is_up and carrier != "0":
                    return True
        return False
    if system == "Darwin":
        ipv4_output = _run(["route", "-n", "get", "default"], timeout)
        if ipv4_output:
            return True
        ipv6_output = _run(["route", "-n", "get", "-inet6", "default"], timeout)
        if ipv6_output:
            return True
        return False if shutil.which("route") else None
    if system == "Windows":
        ipv4_output = _run(["route", "print", "0.0.0.0"], timeout)
        if re.search(
            r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+",
            ipv4_output,
            re.MULTILINE,
        ):
            return True
        ipv6_output = _run(["route", "print", "-6", "::/0"], timeout)
        if re.search(
            r"^\s*\d+\s+\d+\s+::/0\s+\S+",
            ipv6_output,
            re.MULTILINE,
        ):
            return True
        if ipv4_output or ipv6_output or shutil.which("route"):
            return False
        return None
    # Interface discovery cannot prove reachability, but a non-loopback interface is useful on
    # otherwise unsupported systems.  If even that API is absent, leave the signal unknown.
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return None
    return any(name.lower() not in {"lo", "lo0", "loopback"} for _, name in interfaces)


class HostSignalCollector:
    """Thread-safe collector that can derive CPU utilization between snapshots."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        command_timeout_seconds: float = 1.0,
        user_active_window_seconds: float = 60.0,
    ) -> None:
        if command_timeout_seconds <= 0 or not math.isfinite(command_timeout_seconds):
            raise ValueError("command_timeout_seconds must be positive and finite")
        if user_active_window_seconds < 0 or not math.isfinite(user_active_window_seconds):
            raise ValueError("user_active_window_seconds must be finite and non-negative")
        self._clock = clock
        self._timeout = command_timeout_seconds
        self._active_window = user_active_window_seconds
        self._previous_cpu_ticks: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def collect(self) -> HostSignals:
        errors: list[str] = []
        try:
            raw_timestamp = _finite_number(self._clock(), minimum=0.0)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            raw_timestamp = None
            errors.append(f"clock:{type(exc).__name__}")
        timestamp = raw_timestamp if raw_timestamp is not None else 0.0
        if raw_timestamp is None:
            errors.append("malformed:timestamp")

        try:
            battery_percent, on_battery, charging = _battery_snapshot(self._timeout)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            battery_percent, on_battery, charging = None, None, None
            errors.append(f"battery:{type(exc).__name__}")

        try:
            idle = _finite_number(_idle_seconds(self._timeout), minimum=0.0)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            idle = None
            errors.append(f"idle:{type(exc).__name__}")
        user_active = idle <= self._active_window if idle is not None else None

        try:
            thermal_state, temperature = _thermal_snapshot(self._timeout)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            thermal_state, temperature = ThermalState.UNKNOWN, None
            errors.append(f"thermal:{type(exc).__name__}")

        gpu_utilization: float | None = None
        gpu_memory_percent: float | None = None
        try:
            gpu_snapshot = gpu.load_snapshot(timeout=self._timeout)
            # Apple Silicon's existing snapshot intentionally advertises total unified memory but
            # fills used/utilization with zero placeholders.  Unknown is more honest than treating
            # those placeholders as measurements in a host-protection policy.
            placeholder_macos = platform.system() == "Darwin" and bool(gpu_snapshot)
            if not placeholder_macos:
                gpu_utilization = _finite_number(
                    gpu_snapshot.get("gpu_util"), minimum=0.0, maximum=100.0
                )
                total = _finite_number(gpu_snapshot.get("memory_total_mb"), minimum=0.0)
                used = _finite_number(gpu_snapshot.get("memory_used_mb"), minimum=0.0)
                if total and used is not None:
                    gpu_memory_percent = _finite_number(
                        used / total * 100.0, minimum=0.0, maximum=100.0
                    )
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            errors.append(f"gpu:{type(exc).__name__}")

        memory_percent: float | None = None
        try:
            info = host.gather()
            memory_percent = _finite_number(info.memory_percent, minimum=0.0, maximum=100.0)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            errors.append(f"memory:{type(exc).__name__}")

        load_average: float | None = None
        load_per_cpu: float | None = None
        try:
            load_average = _finite_number(os.getloadavg()[0], minimum=0.0)
            if load_average is not None:
                load_per_cpu = load_average / max(os.cpu_count() or 1, 1)
        except (AttributeError, OSError):
            pass

        try:
            with self._lock:
                current_ticks = _cpu_ticks()
                previous_ticks = self._previous_cpu_ticks
                self._previous_cpu_ticks = current_ticks
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            current_ticks = None
            previous_ticks = None
            errors.append(f"cpu:{type(exc).__name__}")
        cpu_utilization: float | None = None
        if current_ticks is not None and previous_ticks is not None:
            idle_delta = current_ticks[0] - previous_ticks[0]
            total_delta = current_ticks[1] - previous_ticks[1]
            if total_delta > 0 and 0 <= idle_delta <= total_delta:
                cpu_utilization = (1.0 - idle_delta / total_delta) * 100.0

        try:
            network = _network_available(self._timeout)
        except Exception as exc:  # noqa: BLE001 - a probe must not take down collection
            network = None
            errors.append(f"network:{type(exc).__name__}")

        return HostSignals(
            timestamp=timestamp,
            battery_percent=battery_percent,
            on_battery=on_battery,
            battery_charging=charging,
            idle_seconds=idle,
            user_active=user_active,
            thermal_state=thermal_state,
            temperature_celsius=temperature,
            gpu_utilization_percent=gpu_utilization,
            gpu_memory_percent=gpu_memory_percent,
            cpu_utilization_percent=cpu_utilization,
            load_average_1m=load_average,
            load_per_cpu=load_per_cpu,
            memory_percent=memory_percent,
            network_available=network,
            collector_errors=tuple(errors),
        )


_DEFAULT_COLLECTOR = HostSignalCollector()


def collect_host_signals() -> HostSignals:
    """Collect one snapshot with the process-wide collector.

    Reusing the collector lets Linux CPU utilization be calculated from tick deltas without adding
    a sleep to every heartbeat.  The first snapshot correctly reports that metric as unknown.
    """

    return _DEFAULT_COLLECTOR.collect()
