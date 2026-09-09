"""The CA signing call must carry its own serial instruction.

OpenSSL 3 silently creates the serial file a `-CA` signing run needs; stock macOS LibreSSL
refuses instead (`ca.srl: No such file or directory`). A second-machine E2E died exactly
there, so the invocation is a tested contract, not a dev-machine coincidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shared import tls


def test_server_cert_signing_passes_cacreateserial(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append([str(part) for part in command])
        # Each real openssl step writes a key and/or an output file the next step reads;
        # materialize them so ensure_server_cert's existence checks see a complete run.
        for flag in ("-keyout", "-out"):
            if flag in command:
                Path(str(command[command.index(flag) + 1])).write_text("material\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    crt, key, ca = tls.ensure_server_cert(tmp_path / "tls", ["192.0.2.10"])
    assert crt.is_file() and key.is_file() and ca.is_file()

    signing = [call for call in calls if "x509" in call and "-req" in call]
    assert len(signing) == 1
    assert "-CAcreateserial" in signing[0]
