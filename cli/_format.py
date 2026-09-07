"""Display formatters for the live-grid readouts (`grid stats`, `grid usage`).

Ported field-for-field from the desktop app's own helpers — `formatCount` /
`formatVram` / `formatVramShare` / `formatShare` / `answeredWindowLabel` in
`autonomous-grid-app` (`lib/features/network/logic/node_metrics.dart`,
`grid_power_provider.dart`, `model_usage.dart`). The two surfaces read the *same*
relay payload, so a grid whose panel says "0.9 / 1.4 TB" and whose terminal says
something else is a bug the eye catches immediately; keeping the rounding rules
identical is what stops that drifting.

Pure and stdlib-only: no `cli` sibling imports, so this stays safe to import at module
top from a module `cli.dispatch` pulls in while the package is still initialising.
"""
from __future__ import annotations

import math


def round_half_up(value: float) -> int:
    """``value`` rounded with .5 going away from zero — Dart's ``num.round()``.

    Not ``round()``: Python's is banker's rounding, so ``round(0.5)`` is 0 and ``round(2.5)``
    is 2. Every figure here is a token count or a percentage read against the app's, and a
    number that disagrees with the panel beside it in the last digit is the kind of difference
    nobody can explain and everybody reports.
    """
    return -int(math.floor(-value + 0.5)) if value < 0 else int(math.floor(value + 0.5))


def metric_number(value: float) -> str:
    """A figure with at most one decimal and no trailing ``.0`` — ``3755``, ``121.7``."""
    rounded = round_half_up(value * 10) / 10
    return f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"


def count(value: int) -> str:
    """A count shortened to something a column can hold — ``940``, ``12.3K``, ``1.2M``, ``1.3B``.

    Token counts span nine orders of magnitude between a quiet machine and a busy grid's daily
    input, so a raw ``1285402913`` would break every table it lands in. The unit steps up while
    the figure would otherwise print four digits (at 999.5, before the rounding below could
    expose a ``1000K``), rather than on fixed thresholds.
    """
    if value < 0:
        return "0"
    scaled = float(value)
    suffix = ""
    for unit in ("K", "M", "B"):
        if scaled < 999.5:
            break
        scaled /= 1000
        suffix = unit
    digits = metric_number(scaled) if scaled < 100 else str(round_half_up(scaled))
    return f"{digits}{suffix}"


def _trim(value: float) -> str:
    """Drop a trailing ``.0`` (``48.0`` → ``48``), keep one decimal otherwise (``47.5``)."""
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def memory(gb: float) -> str:
    """A memory figure — ``192 GB``, ``1.4 TB``.

    Switches to TB past 1024 so a large grid reads as "1.4 TB" rather than a four-digit GB
    number that no longer scans.
    """
    return f"{_trim(gb / 1024)} TB" if gb >= 1024 else f"{_trim(gb)} GB"


def memory_share(used_gb: float, total_gb: float) -> str:
    """``0.9/1.4 TB`` — memory in use against the pool it is drawn from, sharing one unit.

    The unit is written once and chosen from the **total**: two figures either side of a slash
    are being compared, and a comparison written in two different units is a puzzle. Under
    100 GB both halves keep a decimal (on a 63.7 GB card a tenth is a twentieth of the total);
    from three digits up both drop it, because ``276.9/382.4`` spends ten characters on a tenth
    of a gigabyte nobody is acting on.
    """
    if total_gb >= 1024:
        return f"{_trim(used_gb / 1024)}/{_trim(total_gb / 1024)} TB"
    if total_gb < 100:
        return f"{_trim(used_gb)}/{_trim(total_gb)} GB"
    return f"{round_half_up(used_gb)}/{round_half_up(total_gb)} GB"


def share(fraction: float) -> str:
    """A fraction as a percentage, never rounded to a lie.

    A model at 0.4% of the grid would round to ``0%`` under plain integer rounding, which reads
    as "did nothing" against a row plainly showing 24.6K tokens. Below 1% the figure keeps a
    decimal, and anything that would still round to zero says ``<0.1%``.
    """
    pct = fraction * 100
    if pct >= 9.95:
        return f"{round_half_up(pct)}%"
    if pct >= 0.95:
        return f"{metric_number(pct)}%"
    if pct >= 0.05:
        return f"{pct:.1f}%"
    return "<0.1%" if pct > 0 else "0%"


def window(seconds: int) -> str:
    """The span a rollup covers, as short as it can honestly be said — ``24h``, ``7d``, ``30m``.

    Derived from what the relay sent rather than hardcoded: the window is an operator knob, and
    a label reading "24h" while the master counted six would be wrong in the one way a figure
    must never be. Days only from two up — a day is "24h" to anyone talking about what a machine
    did today, and 86400 is the default, so that is the string almost every readout carries.
    """
    if seconds <= 0:
        return ""
    if seconds % 86400 == 0 and seconds >= 172800:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
