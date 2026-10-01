"""Replace what identifies a person, a machine or our infrastructure in a recorded answer, and keep its shape.

This repository is public, so a real answer becomes a recording (`protocol/recordings/`) only after this:

- a machine's name becomes ``node-a``, ``node-b``… (``nodes[].name``, ``last_known.nodes[].name``,
  ``providers[].meta.name``);
- a node id becomes ``grid-000…01``, everywhere — inside route ids and as a mapping key too;
- the per-model hash in a route id or a provider group becomes ``model0000001``;
- an e-mail address becomes ``provider@example.com``, a device label ``example-device``, a provider's own
  address ``https://provider.example.com``.

The same real value gets the same placeholder within one ``Sanitizer``, so the answer still says what it said
("these two routes are one machine's"). A placeholder is left as it is, so sanitizing twice changes nothing more —
``tests/test_protocol_tools.py`` holds every committed recording to that.

It FAILS CLOSED: an IP address or anything that looks like a credential has no placeholder that keeps the answer
honest, so it raises :class:`Unsafe` and a person decides. Model ids and public model names are kept: they are what
the recording is about.

Usage: ``python protocol/tools/sanitize.py RAW.json… --out DIR`` — each RAW file is a recording
(``{schema, definition, request, status, source, recorded_at, body}``) whose body is not sanitized yet.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys
from typing import Any

PROVIDER_EMAIL = "provider@example.com"
DEVICE = "example-device"
PROVIDER_ADDRESS = "https://provider.example.com"

_NODE_ID = re.compile(r"grid-[0-9a-f]{32}")
_ROUTE_HASH = re.compile(r"(provider:grid-[0-9a-f]{32}:|provider-group:)([A-Za-z0-9_-]+)")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CREDENTIAL = re.compile(r"Bearer |lga_sk_|eyJ[A-Za-z0-9_-]{5,}")
_NAME_PLACEHOLDER = re.compile(r"^node-[a-z]+$")
_HASH_PLACEHOLDER = re.compile(r"^model\d{7}$")
#: A placeholder node id is numbered from 1; a real one is 128 random bits.
_MAX_PLACEHOLDER_ID = 0xFFFF


class Unsafe(ValueError):
    """The answer carries something this tool cannot replace safely (an IP address, a credential)."""


def _letters(number: int) -> str:
    """1 → a, 26 → z, 27 → aa: a spreadsheet's column names."""
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


class Sanitizer:
    """One run's replacements: the same real value always gets the same placeholder."""

    def __init__(self) -> None:
        self._node_ids: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._hashes: dict[str, str] = {}

    # ---- one value each -------------------------------------------------------------------------------------

    def _node_id(self, real: str) -> str:
        if int(real[len("grid-"):], 16) <= _MAX_PLACEHOLDER_ID:
            return real
        return self._node_ids.setdefault(real, f"grid-{len(self._node_ids) + 1:032x}")

    def _name(self, real: str) -> str:
        if _NAME_PLACEHOLDER.match(real):
            return real
        return self._names.setdefault(real, f"node-{_letters(len(self._names) + 1)}")

    def _route_hash(self, real: str) -> str:
        if _HASH_PLACEHOLDER.match(real):
            return real
        return self._hashes.setdefault(real, f"model{len(self._hashes) + 1:07d}")

    def _string(self, value: str) -> str:
        if _IPV4.search(value) or _CREDENTIAL.search(value):
            raise Unsafe(f"cannot sanitize {value[:40]!r}: an IP address or a credential has no honest placeholder")
        if _EMAIL.match(value) and not value.endswith("@example.com"):
            return PROVIDER_EMAIL
        value = _ROUTE_HASH.sub(lambda match: match.group(1) + self._route_hash(match.group(2)), value)
        return _NODE_ID.sub(lambda match: self._node_id(match.group(0)), value)

    # ---- the walk -------------------------------------------------------------------------------------------

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._string(value)
        if isinstance(value, list):
            return [self._walk(item) for item in value]
        if isinstance(value, dict):
            return {self._walk(key): self._walk(item) for key, item in value.items()}
        return value

    def _machine(self, node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        out = dict(node)
        if isinstance(out.get("name"), str):
            out["name"] = self._name(out["name"])
        if isinstance(out.get("device"), str) and out["device"]:
            out["device"] = DEVICE
        return out

    def body(self, answer: Any) -> Any:
        """A sanitized copy of ``answer``; ``answer`` itself is not changed.

        The fields a placeholder replaces whole go first, so that an address the answer carries in one of them (a
        provider's ``endpoint_url``) is replaced rather than refused; every other string is then scanned.
        """
        return self._walk(self._fields(copy.deepcopy(answer)))

    def _fields(self, out: Any) -> Any:
        if not isinstance(out, dict):
            return out
        if isinstance(out.get("nodes"), list):
            out["nodes"] = [self._machine(node) for node in out["nodes"]]
        last_known = out.get("last_known")
        if isinstance(last_known, dict) and isinstance(last_known.get("nodes"), list):
            out["last_known"] = {**last_known, "nodes": [self._machine(node) for node in last_known["nodes"]]}
        if isinstance(out.get("providers"), list):
            out["providers"] = [self._provider(provider) for provider in out["providers"]]
        return out

    def _provider(self, provider: Any) -> Any:
        if not isinstance(provider, dict):
            return provider
        out = dict(provider)
        if isinstance(out.get("meta"), dict):
            out["meta"] = self._machine(out["meta"])
        if isinstance(out.get("endpoint_url"), str):
            out["endpoint_url"] = PROVIDER_ADDRESS
        return out

    def recording(self, recording: dict[str, Any]) -> dict[str, Any]:
        """A copy of ``recording`` with ``sanitized: true`` and its body sanitized, in that order, last — one layout
        for every recording, so making one again changes no byte it does not have to."""
        rest = {key: value for key, value in recording.items() if key not in ("sanitized", "body")}
        return {**rest, "sanitized": True, "body": self.body(recording["body"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python protocol/tools/sanitize.py", description=__doc__.splitlines()[0])
    parser.add_argument("raw", nargs="+", type=pathlib.Path, help="unsanitized recordings")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="where the sanitized recordings go")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for path in args.raw:
        recording = Sanitizer().recording(json.loads(path.read_text(encoding="utf-8")))
        target = args.out / path.name
        target.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
