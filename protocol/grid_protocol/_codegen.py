"""Derive grid-protocol's named values (Python) and types (TypeScript) from its JSON Schemas.

The schemas in ``grid_protocol/schemas/`` are the contract (ADR 0004). This module turns them into the two
files every party reads instead of hand-copying literals:

- ``grid_protocol/constants.py`` — each value a schema names with ``x-constant``, each route path named with
  ``x-route``, each literal ``x-names`` lists (a flag, a header name — part of a shape's contract but never
  inside an instance), and ``REFUSAL_STATUS`` (a refusal code → the HTTP status that carries it, from
  ``x-status``; the code is ``properties.code``'s const, or ``properties.error.properties.code``'s for
  OpenAI's error envelope);
- ``protocol/typescript/gridProtocol.ts`` — the same values, plus one TypeScript type per schema and per
  ``$defs`` entry, for the harness daemon (which vendors the file) and ticket 13's conformance driver.

It FAILS CLOSED. A keyword it does not translate is an error, never skipped: a skipped ``allOf`` or ``if``
would emit a type that promises more than the wire carries, which is exactly the silent drift the old
lockstep register existed to catch. Keywords that only narrow a value in ways TypeScript cannot express
(``minimum``, ``pattern``…) are allowed and left to JSON Schema validation.

Run ``python -m grid_protocol._codegen --write`` after changing a schema; ``--check`` exits 1 while a
committed file is stale. ``tests/test_protocol_generated.py`` runs the check in CI.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

SCHEMA_SUFFIX = ".schema.json"
PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
SCHEMA_DIR = PACKAGE_DIR / "schemas"
CONSTANTS_FILE = PACKAGE_DIR / "constants.py"
TYPESCRIPT_FILE = PACKAGE_DIR.parent / "typescript" / "gridProtocol.ts"

#: Keywords that decide what an instance may be, and that the TypeScript emitter translates.
_TRANSLATED = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const", "oneOf", "anyOf",
    "$ref", "$defs",
})
#: Keywords that annotate, or narrow a value in a way a TypeScript type cannot say. Allowed; not translated.
_ANNOTATIONS = frozenset({
    "$schema", "$id", "$comment", "title", "description", "default", "examples", "format", "deprecated",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
    "pattern", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
})
#: This package's own annotations (see the module docstring).
_EXTENSIONS = frozenset({"x-constant", "x-status", "x-route", "x-names", "x-headers"})
_KNOWN = _TRANSLATED | _ANNOTATIONS | _EXTENSIONS

_TYPE_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_CONSTANT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TS_PRIMITIVES = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean",
                  "null": "null"}
_DOC_WIDTH = 110


class UnsupportedSchema(ValueError):
    """A schema uses something this generator does not translate, or breaks one of its naming rules."""


@dataclass(frozen=True)
class _Constant:
    name: str
    value: object
    source: str
    description: str


@dataclass
class _Contract:
    """Every file, validated once; the names each emitter needs."""

    files: dict[str, dict]
    types: dict[str, tuple[str, dict]] = field(default_factory=dict)  # type name -> (file, schema)
    roots: dict[str, str] = field(default_factory=dict)  # file -> the type name of its root
    constants: list[_Constant] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)  # refusal code -> HTTP status


def load_schemas(directory: pathlib.Path = SCHEMA_DIR) -> dict[str, dict]:
    """Every ``*.schema.json`` in ``directory``, keyed by file name."""
    return {path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob(f"*{SCHEMA_SUFFIX}"))}


def _walk(schema: object, where: str) -> Iterator[tuple[dict, str]]:
    """Each subschema with a readable location, document order, the schema itself first."""
    if not isinstance(schema, dict):
        return
    yield schema, where
    for key in ("properties", "$defs"):
        for name, sub in (schema.get(key) or {}).items():
            yield from _walk(sub, f"{where}/{key}/{name}")
    for key in ("items", "additionalProperties"):
        yield from _walk(schema.get(key), f"{where}/{key}")
    for key in ("oneOf", "anyOf"):
        for index, sub in enumerate(schema.get(key) or []):
            yield from _walk(sub, f"{where}/{key}/{index}")


def _check_keywords(schema: dict, where: str) -> None:
    unknown = sorted(set(schema) - _KNOWN)
    if unknown:
        raise UnsupportedSchema(f"{where}: {', '.join(unknown)} is not translated by grid_protocol._codegen")
    if "$ref" in schema and set(schema) & (_TRANSLATED - {"$ref"}):
        raise UnsupportedSchema(f"{where}: $ref beside another translated keyword is not supported")


def _register_type(contract: _Contract, name: str, file: str, schema: dict) -> None:
    if not _TYPE_NAME.match(name):
        raise UnsupportedSchema(f"{file}: type name {name!r} must be PascalCase")
    if name in contract.types:
        raise UnsupportedSchema(f"type {name} is defined twice ({contract.types[name][0]} and {file})")
    contract.types[name] = (file, schema)


def _add_constant(contract: _Contract, name: str, value: object, where: str, description: str) -> None:
    if not _CONSTANT_NAME.match(name):
        raise UnsupportedSchema(f"{where}: constant name {name!r} must be UPPER_SNAKE_CASE")
    if any(existing.name == name for existing in contract.constants):
        raise UnsupportedSchema(f"{where}: constant {name} is named twice")
    contract.constants.append(_Constant(name, value, where, description))


def _collect_values(contract: _Contract, schema: dict, where: str) -> None:
    description = schema.get("description", "")
    if "x-constant" in schema:
        if "const" not in schema:
            raise UnsupportedSchema(f"{where}: x-constant needs a const beside it")
        _add_constant(contract, schema["x-constant"], schema["const"], where, description)
    route = schema.get("x-route")
    if route is not None:
        if not isinstance(route, dict) or set(route) != {"method", "path", "constant"}:
            raise UnsupportedSchema(f"{where}: x-route must be {{method, path, constant}}")
        _add_constant(contract, route["constant"], route["path"], where, f"{route['method']} {route['path']}")
    for name, value in _mapping(schema, "x-names", where, (str, int, float)).items():
        _add_constant(contract, name, value, where, "")
    _mapping(schema, "x-headers", where, (str,))
    if "x-status" in schema:
        code = _refusal_code(schema)
        if not isinstance(code, str) or not isinstance(schema["x-status"], int):
            raise UnsupportedSchema(f"{where}: x-status needs an integer beside a properties.code const")
        if code in contract.statuses:
            raise UnsupportedSchema(f"{where}: refusal code {code} has two statuses")
        contract.statuses[code] = schema["x-status"]


def _mapping(schema: dict, key: str, where: str, value_types: tuple[type, ...]) -> dict:
    """``schema[key]`` when it is a mapping of names to values of ``value_types``; ``{}`` when absent."""
    value = schema.get(key, {})
    if not isinstance(value, dict) or not all(
            isinstance(name, str) and isinstance(item, value_types) and not isinstance(item, bool)
            for name, item in value.items()):
        kinds = " or ".join(kind.__name__ for kind in value_types)
        raise UnsupportedSchema(f"{where}: {key} must map each name to a {kinds}")
    return value


def _refusal_code(schema: dict) -> object:
    """A refusal's code: ``properties.code``'s const, or inside OpenAI's ``error`` envelope."""
    properties = schema.get("properties") or {}
    if "code" in properties:
        return properties["code"].get("const")
    return ((properties.get("error") or {}).get("properties") or {}).get("code", {}).get("const")


def _build(files: Mapping[str, dict]) -> _Contract:
    contract = _Contract(files={name: files[name] for name in sorted(files)})
    for file, root in contract.files.items():
        if not file.endswith(SCHEMA_SUFFIX):
            raise UnsupportedSchema(f"{file}: a schema file's name ends in {SCHEMA_SUFFIX}")
        _register_type(contract, str(root.get("title", "")), file, root)
        contract.roots[file] = root["title"]
        for name, sub in (root.get("$defs") or {}).items():
            _register_type(contract, name, file, sub)
        for schema, where in _walk(root, file):
            _check_keywords(schema, where)
            _collect_values(contract, schema, where)
    for file, root in contract.files.items():
        for schema, where in _walk(root, file):
            if "$ref" in schema:
                _resolve(contract, file, schema["$ref"], where)
    return contract


def _resolve(contract: _Contract, file: str, ref: str, where: str) -> str:
    """The type name a ``$ref`` points at: ``#/$defs/X``, ``other.schema.json`` or ``other.schema.json#/$defs/X``."""
    target_file, _, fragment = ref.partition("#")
    target_file = target_file or file
    if target_file not in contract.files:
        raise UnsupportedSchema(f"{where}: $ref {ref!r} names no schema file")
    if not fragment:
        return contract.roots[target_file]
    match = re.fullmatch(r"/\$defs/([A-Za-z0-9]+)", fragment)
    if not match or match.group(1) not in (contract.files[target_file].get("$defs") or {}):
        raise UnsupportedSchema(f"{where}: $ref {ref!r} points at nothing")
    return match.group(1)


# ---- Python ------------------------------------------------------------------------------------------------

def _py_literal(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise UnsupportedSchema(f"a named value must be a string, number, boolean or null, not {value!r}")


def _py_comment(text: str) -> list[str]:
    words = " ".join(text.split())
    return [f"#: {line}" for line in _wrap(words, _DOC_WIDTH - 3)] if words else []


def python_constants(files: Mapping[str, dict], version: str) -> str:
    contract = _build(files)
    lines = [
        f'"""GENERATED by grid-protocol {version} from `grid_protocol/schemas/` — do not edit.',
        "",
        "Change a schema, then run `python -m grid_protocol._codegen --write`. Each value's comment is the",
        "description the schema gives it; the schema says the rest.",
        '"""',
    ]
    if contract.statuses:
        lines += ["from types import MappingProxyType"]
    lines += ["", f"PROTOCOL_VERSION = {_py_literal(version)}", ""]
    for constant in contract.constants:
        lines += _py_comment(f"{constant.description} ({constant.source})".strip())
        lines += [f"{constant.name} = {_py_literal(constant.value)}", ""]
    if contract.statuses:
        lines += _py_comment("Each refusal code, and the HTTP status that carries it.")
        lines += ["REFUSAL_STATUS = MappingProxyType({"]
        lines += [f"    {_py_literal(code)}: {status}," for code, status in contract.statuses.items()]
        lines += ["})", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---- TypeScript --------------------------------------------------------------------------------------------

def _ts_literal(value: object) -> str:
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    raise UnsupportedSchema(f"a literal must be a string, number, boolean or null, not {value!r}")


def _ts_key(name: str) -> str:
    return name if _IDENTIFIER.match(name) else _ts_literal(name)


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    return lines + ([current] if current else [])


def _ts_doc(description: str, indent: str) -> list[str]:
    text = " ".join(description.split()).replace("*/", "*\\/")
    if not text:
        return []
    if len(indent) + len(text) + 7 <= _DOC_WIDTH:
        return [f"{indent}/** {text} */"]
    body = [f"{indent} * {line}" for line in _wrap(text, _DOC_WIDTH - len(indent) - 3)]
    return [f"{indent}/**", *body, f"{indent} */"]


def _union(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        for piece in part.split(" | ") if not part.startswith("{") else [part]:
            if piece not in seen:
                seen.append(piece)
    return " | ".join(seen)


def _ts_array(schema: dict, contract: _Contract, file: str, indent: str) -> str:
    items = schema.get("items")
    inner = _ts_type(items, contract, file, indent) if isinstance(items, dict) else "unknown"
    simple = re.fullmatch(r"[A-Za-z0-9_]+", inner)
    return f"{inner}[]" if simple else f"Array<{inner}>"


def _ts_single(kind: str, schema: dict, contract: _Contract, file: str, indent: str) -> str:
    if kind == "object":
        return "{\n" + "\n".join(_ts_members(schema, contract, file, indent + "  ")) + f"\n{indent}}}"
    if kind == "array":
        return _ts_array(schema, contract, file, indent)
    if kind not in _TS_PRIMITIVES:
        raise UnsupportedSchema(f"{file}: type {kind!r} is not a JSON Schema type")
    return _TS_PRIMITIVES[kind]


def _ts_type(schema: dict, contract: _Contract, file: str, indent: str) -> str:
    if "$ref" in schema:
        return _resolve(contract, file, schema["$ref"], file)
    if "const" in schema:
        return _ts_literal(schema["const"])
    if "enum" in schema:
        return _union([_ts_literal(value) for value in schema["enum"]])
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return _union([_ts_type(sub, contract, file, indent) for sub in schema[key]])
    kinds = schema.get("type")
    if kinds is None:
        return "{\n" + "\n".join(_ts_members(schema, contract, file, indent + "  ")) + f"\n{indent}}}" \
            if "properties" in schema else "unknown"
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return _union([_ts_single(kind, schema, contract, file, indent) for kind in kinds])


def _ts_members(schema: dict, contract: _Contract, file: str, indent: str) -> list[str]:
    required = set(schema.get("required") or [])
    lines: list[str] = []
    for name, sub in (schema.get("properties") or {}).items():
        lines += _ts_doc(sub.get("description", ""), indent)
        optional = "" if name in required else "?"
        lines.append(f"{indent}{_ts_key(name)}{optional}: {_ts_type(sub, contract, file, indent)}")
    extra = schema.get("additionalProperties", True)
    if isinstance(extra, dict):
        if schema.get("properties"):
            raise UnsupportedSchema(f"{file}: additionalProperties as a schema beside properties is not supported")
        lines.append(f"{indent}[key: string]: {_ts_type(extra, contract, file, indent)}")
    elif extra is not False:
        lines.append(f"{indent}[key: string]: unknown")
    return lines


def _ts_declaration(name: str, file: str, schema: dict, contract: _Contract) -> list[str]:
    lines = _ts_doc(schema.get("description", ""), "")
    is_interface = (schema.get("type") == "object" or ("properties" in schema and "type" not in schema)) \
        and not ({"oneOf", "anyOf", "$ref", "const", "enum"} & set(schema))
    if is_interface:
        return [*lines, f"export interface {name} {{", *_ts_members(schema, contract, file, "  "), "}"]
    return [*lines, f"export type {name} = {_ts_type(schema, contract, file, '')}"]


def typescript(files: Mapping[str, dict], version: str) -> str:
    contract = _build(files)
    lines = [
        f"// GENERATED by grid-protocol {version} — do not edit. The source is autonomous-grid's",
        "// `protocol/grid_protocol/schemas/`: change a schema there, run `python -m grid_protocol._codegen --write`,",
        "// and copy this file.",
        "",
        f"export const PROTOCOL_VERSION = {_ts_literal(version)}",
    ]
    for constant in contract.constants:
        lines += ["", *_ts_doc(constant.description, ""),
                  f"export const {constant.name} = {_ts_literal(constant.value)}"]
    if contract.statuses:
        lines += ["", *_ts_doc("Each refusal code, and the HTTP status that carries it.", ""),
                  "export const REFUSAL_STATUS = {",
                  *[f"  {_ts_key(code)}: {status}," for code, status in contract.statuses.items()],
                  "} as const"]
    for name, (file, schema) in contract.types.items():
        lines += ["", *_ts_declaration(name, file, schema, contract)]
    return "\n".join(lines) + "\n"


# ---- the command ---------------------------------------------------------------------------------------------

def _outputs(version: str) -> dict[pathlib.Path, str]:
    files = load_schemas()
    return {CONSTANTS_FILE: python_constants(files, version), TYPESCRIPT_FILE: typescript(files, version)}


def main(argv: list[str] | None = None) -> int:
    from grid_protocol import __version__

    parser = argparse.ArgumentParser(prog="python -m grid_protocol._codegen", description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the generated files")
    mode.add_argument("--check", action="store_true", help="exit 1 while a generated file is stale")
    args = parser.parse_args(argv)
    stale = [path for path, text in _outputs(__version__).items()
             if not path.exists() or path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in stale:
            print(f"stale: {path}", file=sys.stderr)
        return 1 if stale else 0
    for path, text in _outputs(__version__).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
