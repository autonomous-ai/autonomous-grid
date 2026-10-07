"""The generator that turns `grid-protocol`'s JSON Schemas into Python constants and TypeScript types.

The schemas under ``protocol/grid_protocol/schemas/`` are the source of truth (the grid-platform review's
ADR 0004). Two artefacts are
derived from them and committed: ``grid_protocol/constants.py`` (the named values every party imports) and
``protocol/typescript/gridProtocol.ts`` (the types and values the harness daemon vendors). These tests pin the
generator's rules on small synthetic schemas, so a rule is readable here without reading the real contract.

The generator FAILS CLOSED: a JSON Schema keyword it does not translate is an error, never skipped. A skipped
``allOf`` or ``if`` would emit a TypeScript type that promises more than the wire carries — the same silent
drift the lockstep register exists to stop.
"""
from __future__ import annotations

import re

import pytest
from grid_protocol import _codegen


def _schemas(**files: dict) -> dict[str, dict]:
    return {f"{name.replace('_', '-')}.schema.json": body for name, body in files.items()}


REFUSAL = {
    "title": "Refusal",
    "description": "A coded refusal.",
    "type": "object",
    "required": ["detail"],
    "properties": {
        "detail": {"type": "string", "description": "A sentence for a person."},
        "code": {"type": ["string", "null"]},
    },
    "$defs": {
        "GridAsleep": {
            "type": "object",
            "required": ["detail", "code"],
            "properties": {
                "detail": {"type": "string"},
                "code": {"const": "grid_asleep", "x-constant": "GRID_ASLEEP_CODE"},
            },
            "x-status": 503,
        },
    },
}


def test_an_object_becomes_an_interface_that_tolerates_keys_it_does_not_name():
    ts = _codegen.typescript(_schemas(refusal=REFUSAL), version="0.0.0")

    assert "export interface Refusal {" in ts
    assert "  detail: string" in ts
    assert "  code?: string | null" in ts
    # A reader must accept a key a newer server adds; only `additionalProperties: false` closes a shape.
    assert "  [key: string]: unknown" in ts


def test_a_closed_object_has_no_index_signature():
    closed = {"title": "Closed", "type": "object", "properties": {"a": {"type": "integer"}},
              "additionalProperties": False}

    ts = _codegen.typescript(_schemas(closed=closed), version="0.0.0")

    assert "export interface Closed {\n  a?: number\n}" in ts


def test_descriptions_become_doc_comments():
    ts = _codegen.typescript(_schemas(refusal=REFUSAL), version="0.0.0")

    assert "/** A coded refusal. */\nexport interface Refusal {" in ts
    assert "  /** A sentence for a person. */\n  detail: string" in ts


def test_a_const_carrying_x_constant_is_a_named_value_in_both_languages():
    schemas = _schemas(refusal=REFUSAL)

    assert "export const GRID_ASLEEP_CODE = 'grid_asleep'" in _codegen.typescript(schemas, version="0.0.0")
    assert 'GRID_ASLEEP_CODE = "grid_asleep"' in _codegen.python_constants(schemas, version="0.0.0")


def test_a_refusal_definition_puts_its_code_and_status_in_one_table():
    schemas = _schemas(refusal=REFUSAL)

    assert "export const REFUSAL_STATUS = {\n  grid_asleep: 503,\n} as const" in _codegen.typescript(
        schemas, version="0.0.0")
    python = _codegen.python_constants(schemas, version="0.0.0")
    assert 'REFUSAL_STATUS = MappingProxyType({\n    "grid_asleep": 503,\n})' in python


def test_a_route_annotation_is_a_named_path():
    overview = {"title": "Overview", "type": "object",
                "x-route": {"method": "GET", "path": "/relay/v1/grid/overview", "constant": "OVERVIEW_PATH"}}

    schemas = _schemas(overview=overview)

    assert "export const OVERVIEW_PATH = '/relay/v1/grid/overview'" in _codegen.typescript(schemas, version="0.0.0")
    assert 'OVERVIEW_PATH = "/relay/v1/grid/overview"' in _codegen.python_constants(schemas, version="0.0.0")


def test_references_resolve_within_and_across_files():
    node = {"title": "Node", "type": "object", "properties": {"name": {"type": "string"}}}
    listing = {
        "title": "Listing",
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"$ref": "node.schema.json"}},
            "first": {"$ref": "#/$defs/Pair"},
        },
        "$defs": {"Pair": {"type": "array", "items": {"type": ["string", "integer"]}}},
    }

    ts = _codegen.typescript(_schemas(node=node, listing=listing), version="0.0.0")

    assert "  nodes?: Node[]" in ts
    assert "  first?: Pair" in ts
    assert "export type Pair = Array<string | number>" in ts


def test_enum_and_one_of_become_unions():
    choice = {"title": "Choice", "oneOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]}

    ts = _codegen.typescript(_schemas(choice=choice), version="0.0.0")

    assert "export type Choice = 'a' | 'b' | null" in ts


def test_a_map_of_one_shape_is_an_index_signature():
    table = {"title": "Table", "type": "object", "additionalProperties": {"type": "integer"}}

    ts = _codegen.typescript(_schemas(table=table), version="0.0.0")

    assert "export interface Table {\n  [key: string]: number\n}" in ts


def test_a_key_that_is_not_an_identifier_is_quoted():
    odd = {"title": "Odd", "type": "object", "properties": {"x-api-key": {"type": "string"}}}

    ts = _codegen.typescript(_schemas(odd=odd), version="0.0.0")

    assert "  'x-api-key'?: string" in ts


@pytest.mark.parametrize("keyword", ["allOf", "if", "not", "patternProperties", "dependentSchemas", "$dynamicRef"])
def test_a_keyword_the_generator_does_not_translate_is_an_error(keyword):
    schema = {"title": "Bad", "type": "object", keyword: {}}

    with pytest.raises(_codegen.UnsupportedSchema, match=re.escape(keyword)):
        _codegen.typescript(_schemas(bad=schema), version="0.0.0")


def test_a_named_value_without_a_const_is_an_error():
    schema = {"title": "Bad", "type": "object", "properties": {"a": {"type": "string", "x-constant": "A"}}}

    with pytest.raises(_codegen.UnsupportedSchema, match="x-constant"):
        _codegen.python_constants(_schemas(bad=schema), version="0.0.0")


def test_two_values_under_one_name_are_an_error():
    one = {"title": "One", "type": "object", "properties": {"a": {"const": 1, "x-constant": "SAME"}}}
    two = {"title": "Two", "type": "object", "properties": {"b": {"const": 2, "x-constant": "SAME"}}}

    with pytest.raises(_codegen.UnsupportedSchema, match="SAME"):
        _codegen.python_constants(_schemas(one=one, two=two), version="0.0.0")


def test_two_types_under_one_name_are_an_error():
    one = {"title": "One", "type": "object", "$defs": {"Twin": {"type": "string"}}}
    two = {"title": "Two", "type": "object", "$defs": {"Twin": {"type": "integer"}}}

    with pytest.raises(_codegen.UnsupportedSchema, match="Twin"):
        _codegen.typescript(_schemas(one=one, two=two), version="0.0.0")


def test_a_reference_to_nothing_is_an_error():
    schema = {"title": "Dangling", "type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}}

    with pytest.raises(_codegen.UnsupportedSchema, match="Missing"):
        _codegen.typescript(_schemas(dangling=schema), version="0.0.0")


def test_the_output_names_its_version_and_says_it_is_generated():
    schemas = _schemas(refusal=REFUSAL)

    ts = _codegen.typescript(schemas, version="9.8.7")
    python = _codegen.python_constants(schemas, version="9.8.7")

    assert ts.startswith("// GENERATED by grid-protocol 9.8.7")
    assert "do not edit" in ts.splitlines()[0] + ts.splitlines()[1]
    assert "GENERATED by grid-protocol 9.8.7" in python.splitlines()[0]
    assert "export const PROTOCOL_VERSION = '9.8.7'" in ts
    assert 'PROTOCOL_VERSION = "9.8.7"' in python


def test_the_output_does_not_depend_on_the_order_files_are_given_in():
    node = {"title": "Node", "type": "object", "properties": {"name": {"const": "n", "x-constant": "N"}}}
    schemas = _schemas(refusal=REFUSAL, node=node)
    reversed_schemas = dict(reversed(list(schemas.items())))

    assert _codegen.typescript(schemas, version="1") == _codegen.typescript(reversed_schemas, version="1")
    assert _codegen.python_constants(schemas, version="1") == _codegen.python_constants(reversed_schemas, version="1")


def test_the_generated_python_is_importable_and_its_table_is_read_only():
    namespace: dict[str, object] = {}

    exec(_codegen.python_constants(_schemas(refusal=REFUSAL), version="0.0.0"), namespace)  # noqa: S102

    assert namespace["GRID_ASLEEP_CODE"] == "grid_asleep"
    with pytest.raises(TypeError):
        namespace["REFUSAL_STATUS"]["grid_asleep"] = 200  # type: ignore[index]


def test_a_refusal_in_an_error_envelope_finds_its_code_one_level_down():
    # `no_providers_available` and `relay_restarting` travel in OpenAI's `{"error": {..., "code"}}` envelope.
    envelope = {
        "title": "Envelope",
        "type": "object",
        "$defs": {
            "Restarting": {
                "type": "object",
                "properties": {"error": {"type": "object", "properties": {"code": {"const": "relay_restarting"}}}},
                "x-status": 503,
            },
        },
    }

    python = _codegen.python_constants(_schemas(envelope=envelope), version="0.0.0")

    assert '"relay_restarting": 503,' in python


def test_x_names_names_a_literal_that_belongs_to_a_shape_but_not_to_an_instance():
    envelope = {"title": "Envelope", "type": "object",
                "x-names": {"NO_WAKE_FLAG": "--no-wake", "RETRY_AFTER_HEADER": "Retry-After"}}

    schemas = _schemas(envelope=envelope)

    assert 'NO_WAKE_FLAG = "--no-wake"' in _codegen.python_constants(schemas, version="0.0.0")
    assert "export const RETRY_AFTER_HEADER = 'Retry-After'" in _codegen.typescript(schemas, version="0.0.0")


def test_x_headers_documents_headers_and_must_map_a_name_to_a_sentence():
    good = {"title": "Good", "type": "object", "x-headers": {"Retry-After": "Seconds, when present."}}
    bad = {"title": "Bad", "type": "object", "x-headers": {"Retry-After": {"type": "string"}}}

    _codegen.typescript(_schemas(good=good), version="0.0.0")
    with pytest.raises(_codegen.UnsupportedSchema, match="x-headers"):
        _codegen.typescript(_schemas(bad=bad), version="0.0.0")


def test_a_union_of_arrays_of_unions_stays_valid_typescript():
    # Found in review: members were de-duplicated by splitting the rendered text on " | ", which also cut
    # inside `Array<…>` and emitted `Array<string | number> | Array<boolean`.
    pair = {"title": "Pair", "oneOf": [
        {"type": "array", "items": {"type": ["string", "integer"]}},
        {"type": "array", "items": {"type": ["boolean", "integer"]}},
        {"type": ["string", "null"]},
        {"type": "null"},
    ]}

    ts = _codegen.typescript(_schemas(pair=pair), version="0.0.0")

    assert "export type Pair = Array<string | number> | Array<boolean | number> | string | null\n" in ts
