"""grid-protocol — the wire contract between the grid relay, the public `grid` CLI and the harness.

Its decision record is ADR 0004 of the grid-platform review ("the wire contract lives in the public repo"),
not this repository's ``docs/adr/0004``.

The JSON Schemas in ``grid_protocol/schemas/`` are the source of truth. Import the named values from
``grid_protocol.constants`` (generated from the schemas; plain Python, no data files, no dependencies). Load
a schema with :func:`load_schema`, or validate an answer with :func:`validator`, which needs the
``validate`` extra (``jsonschema``).

A change to a shape lands HERE first, then in the parties that speak it — the public CLI is the one party
that cannot be upgraded at once, so its released versions are where compatibility is judged.
"""
from __future__ import annotations

import copy
import functools
import json
from importlib import resources
from typing import Any

__version__ = "0.1.0"

#: Every schema's ``$id`` is this base plus ``<name>.schema.json``. An identifier, not a place to fetch from.
SCHEMA_BASE_URI = "https://grid.autonomous.ai/protocol/"
_SUFFIX = ".schema.json"


@functools.cache
def _raw() -> dict[str, dict[str, Any]]:
    folder = resources.files("grid_protocol") / "schemas"
    return {entry.name[: -len(_SUFFIX)]: json.loads(entry.read_text(encoding="utf-8"))
            for entry in sorted(folder.iterdir(), key=lambda entry: entry.name) if entry.name.endswith(_SUFFIX)}


def schema_names() -> list[str]:
    """Each schema's name — its file name without ``.schema.json`` — sorted."""
    return list(_raw())


def load_schema(name: str) -> dict[str, Any]:
    """The schema called ``name``, as a fresh copy the caller may change."""
    schemas = _raw()
    if name not in schemas:
        raise KeyError(f"no schema {name!r}; the schemas are {', '.join(schemas)}")
    return copy.deepcopy(schemas[name])


def validator(name: str, definition: str | None = None):
    """A Draft 2020-12 validator for schema ``name`` — or for its ``$defs`` entry ``definition`` — that follows
    ``$ref`` into the other schemas.

    A schema's root is the general shape; a definition is one case of it. ``validator("refusal")`` accepts any
    coded refusal, ``validator("refusal", "GridAsleep")`` only a sleeping grid's answer, ``last_known``
    included. Needs ``jsonschema`` (``pip install 'grid-protocol[validate]'``).
    """
    import jsonschema
    from referencing import Registry, Resource

    root = load_schema(name)
    if definition is not None and definition not in root.get("$defs", {}):
        raise KeyError(f"schema {name!r} defines no {definition!r}; it defines {', '.join(root.get('$defs', {}))}")
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in map(load_schema, schema_names()))
    target = root if definition is None else {"$ref": f"{root['$id']}#/$defs/{definition}"}
    return jsonschema.Draft202012Validator(target, registry=registry)
