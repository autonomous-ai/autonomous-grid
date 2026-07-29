"""The CLI seats this grid can serve. Adding one is a new module plus a line here."""
from __future__ import annotations

from . import claude, codex

SEATS = {spec.kind: spec for spec in (claude.SPEC, codex.SPEC)}


def seat_for(kind):
    """The driver for ``kind``, or an error naming what is available."""
    spec = SEATS.get(kind)
    if spec is None:
        from shared.agent.cli_seat import SeatError

        raise SeatError(
            f"No CLI seat driver for kind {kind!r}. Supported: {', '.join(sorted(SEATS)) or 'none'}."
        )
    return spec
