"""Read a value a sibling repository defines, whether it writes the literal or takes it from `grid-protocol`.

Since grid-platform ticket 12 grid-src imports its contract values (`from grid_protocol import constants as
protocol`) instead of hand-copying them, so a pin that parses grid-src's source finds ``protocol.BOOT_HOLD_SECONDS``
where it used to find ``30.0``. A pin that read only literals would report that MOVE as a deletion — exactly the
false red this repository's CLAUDE.md warns about. This resolves both spellings to the value, so the pin keeps
checking the number either way.
"""
from __future__ import annotations

import ast

from grid_protocol import constants

#: The name the siblings import the constants under.
PROTOCOL_ALIAS = "protocol"
_CONVERSIONS = {"float": float, "int": int, "str": str}


def protocol_value(node: ast.AST) -> object:
    """The value of ``node``: a literal, ``protocol.NAME``, or ``float|int|str(<one of those>)``.

    Anything else raises, naming what was found — a pin must be taught a new spelling, never skip it.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -protocol_value(node.operand)  # type: ignore[operator]
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == PROTOCOL_ALIAS):
        if not hasattr(constants, node.attr):
            raise AssertionError(f"`{PROTOCOL_ALIAS}.{node.attr}` is not a grid-protocol constant")
        return getattr(constants, node.attr)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CONVERSIONS
            and len(node.args) == 1 and not node.keywords):
        return _CONVERSIONS[node.func.id](protocol_value(node.args[0]))
    raise AssertionError(f"not a literal and not a grid-protocol value: {ast.unparse(node)}")
