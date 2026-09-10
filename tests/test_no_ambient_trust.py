"""Keep the removed trust machinery removed.

Two helpers and one fetch are gone from local mode, and each caused a production bug:

- `SSL_CERT_FILE` (via `server_tls_client_env` / `apply_server_tls_client_env`) REPLACED the
  platform trust store for the whole process rather than adding to it. Every CA this codebase
  minted carried the identical subject, so two grids on one machine put two different keys under
  one name and OpenSSL -- which looks an issuer up BY SUBJECT -- verified a leaf against the
  wrong one. MEASURED as "certificate signature failure" on a working pair.
- `_fetch_grid_ca` learned a CA over `verify=False` on the first verification failure, and
  re-armed on every later one. A same-network attacker who could fail one request could serve
  their own CA to the retry.

Nothing needs them now: local mode serves plain HTTP unless an operator asks for TLS, and a
caller that does verify passes the CA explicitly instead of installing it process-wide.
"""

import io
import pathlib
import tokenize

BANNED = (
    "apply_server_tls_client_env",
    "server_tls_client_env",
    "_fetch_grid_ca",
    "SSL_CERT_FILE",
)


def _code_without_prose(source: str) -> str:
    """The source with comments and docstrings dropped.

    A plain substring scan cannot tell a module USING one of these from a module EXPLAINING why
    it no longer does -- and the explanation is worth keeping, since a reader who does not know
    what SSL_CERT_FILE did wrong is one step from reintroducing it.
    """

    kept: list[str] = []
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
            tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT,
        ):
            continue  # a docstring: the only string that stands alone as a statement
        kept.append(token.string)
        if token.type not in (tokenize.NL, tokenize.COMMENT):
            previous = token.type
    return "\n".join(kept)


def test_no_module_reinstates_the_removed_trust_machinery():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(root)}: {name}"
        for path in sorted(
            list((root / "cli").rglob("*.py")) + list((root / "local").rglob("*.py"))
        )
        for name in BANNED
        if name in _code_without_prose(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
