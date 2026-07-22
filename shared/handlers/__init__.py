"""Registry of vendor handlers, keyed by api_kind.

Each entry maps an api_kind (from the run record) to a handler class whose
constructor takes (base_url, api_key) and exposes a forward(body, endpoint)
method that yields SSE data lines.

Adding a new vendor: implement the handler, then add it to HANDLERS below.
"""
from __future__ import annotations

from shared.handlers.doggi import DoggiHandler

HANDLERS: dict[str, type] = {
    "doggi": DoggiHandler,
}