"""`grid-protocol` as a package: its schemas load, validate each other, and its generated files are current.

These run in this repository's CI on every change — unlike the cross-repo lockstep pins, they need no
sibling checkout. The contract's CONTENT (what each shape carries, and the real answers it was checked
against) is pinned by `tests/test_protocol_contract.py`.
"""
from __future__ import annotations

import json

import grid_protocol
import jsonschema
import pytest
from grid_protocol import _codegen



def test_every_schema_is_a_valid_draft_2020_12_schema():
    names = grid_protocol.schema_names()

    assert names, "grid_protocol/schemas/ holds no schema"
    for name in names:
        jsonschema.Draft202012Validator.check_schema(grid_protocol.load_schema(name))


def test_each_schema_declares_the_dialect_and_an_id_under_one_base():
    for name in grid_protocol.schema_names():
        schema = grid_protocol.load_schema(name)

        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", name
        assert schema["$id"] == f"{grid_protocol.SCHEMA_BASE_URI}{name}{_codegen.SCHEMA_SUFFIX}", name


def test_a_loaded_schema_is_a_fresh_copy():
    name = grid_protocol.schema_names()[0]
    first = grid_protocol.load_schema(name)

    first["title"] = "changed by a caller"

    assert grid_protocol.load_schema(name)["title"] != "changed by a caller"


def test_an_unknown_schema_name_says_which_names_exist():
    with pytest.raises(KeyError, match="refusal"):
        grid_protocol.load_schema("no-such-shape")


def test_a_validator_follows_references_across_files():
    # The asleep refusal carries `last_known`, which is defined in its own file.
    validator = grid_protocol.validator("refusal", "GridAsleep")
    answer = {
        "detail": "This grid is resting.",
        "code": "grid_asleep",
        "last_known": {"age_seconds": "not a number", "nodes": [], "ids": []},
    }

    errors = list(validator.iter_errors(answer))

    assert errors, "a last_known whose age is a string validated — the cross-file $ref was not followed"


def test_the_generated_files_match_the_schemas():
    assert _codegen.main(["--check"]) == 0, (
        "grid_protocol/constants.py or protocol/typescript/gridProtocol.ts is stale — run "
        "`python -m grid_protocol._codegen --write` and commit both")


def test_the_package_version_is_the_one_the_generated_files_carry():
    from grid_protocol import constants

    assert constants.PROTOCOL_VERSION == grid_protocol.__version__
    assert f"export const PROTOCOL_VERSION = '{grid_protocol.__version__}'" in _codegen.TYPESCRIPT_FILE.read_text()


def test_the_distribution_version_is_the_package_version():
    pyproject = (_codegen.PACKAGE_DIR.parent / "pyproject.toml").read_text()

    assert f'version = "{grid_protocol.__version__}"' in pyproject


def test_the_schemas_ship_inside_the_package():
    # The wheel's package data is `grid_protocol/schemas/*.schema.json`; a schema anywhere else would be
    # missing from every install while every test here, reading the source tree, still passed.
    built = {"build", "dist"}  # what `uv build` leaves behind locally holds copies; it never ships from here
    for path in _codegen.SCHEMA_DIR.parent.parent.rglob(f"*{_codegen.SCHEMA_SUFFIX}"):
        if built & set(path.parts) or any(part.endswith(".egg-info") for part in path.parts):
            continue
        assert path.parent == _codegen.SCHEMA_DIR, f"{path} is outside grid_protocol/schemas/"
    json.loads((_codegen.SCHEMA_DIR / f"refusal{_codegen.SCHEMA_SUFFIX}").read_text())


def test_a_definition_validates_only_its_own_case():
    stopped = {"detail": "Its owner stopped this grid.", "code": "grid_stopped"}

    assert grid_protocol.validator("refusal").is_valid(stopped)
    assert not grid_protocol.validator("refusal", "GridAsleep").is_valid(stopped)
    with pytest.raises(KeyError, match="GridAsleep"):
        grid_protocol.validator("refusal", "NoSuchCase")
