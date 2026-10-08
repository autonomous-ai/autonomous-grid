"""The Linux binaries run on every glibc from the one they were BUILT on — so they are built on an old one.

A Nuitka binary links against the C library of the machine that compiled it, and the dynamic linker refuses to start
it on an older one. Built on GitHub's ``ubuntu-latest`` (24.04, glibc 2.39), every release from at least v0.3.47 to
v0.3.56 needed ``GLIBC_2.38`` in its onefile bootstrap itself: ``grid --version`` died on Debian 12 (2.36), Ubuntu
22.04 (2.35) and RHEL 9 (2.34), and ``install.sh`` reported a grid that "installed but failed to run". Measured
2026-10-08: the same source built in ``python:3.12-bullseye`` (2.31) runs on Debian 11 and 12, Ubuntu 20.04 and 22.04
and Rocky 9.

So the binaries job builds inside that image, and runs what it built there before uploading it: a binary that cannot
start on its own build image's glibc never reaches a release.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

#: The build images allowed, and the glibc each one is — the oldest glibc the binaries then run on.
OLD_GLIBC_IMAGES = {"python:3.12-bullseye": "2.31"}


def _binaries_job() -> str:
    """The text of the `binaries:` job, up to the next job at the same indent."""
    text = WORKFLOW.read_text()
    start = text.index("\n  binaries:\n")
    following = re.search(r"\n  [a-z][a-z0-9_-]*:\n", text[start + 1:])
    return text[start:start + 1 + following.start()] if following else text[start:]


def test_the_binaries_are_built_on_an_old_glibc():
    image = re.search(r"\n    container:\s*(?:\n\s+image:\s*)?([^\s#]+)", _binaries_job())
    assert image, "the binaries job has no build container: it compiles against the runner's own (new) glibc"
    assert image.group(1) in OLD_GLIBC_IMAGES, (
        f"build image {image.group(1)!r}: its glibc is the oldest the binaries run on, so it must be one of "
        f"{sorted(OLD_GLIBC_IMAGES)}"
    )


def test_the_job_runs_what_it_built_before_uploading_it():
    job = _binaries_job()
    ran = re.search(r'\./grid-\$\{\{ matrix\.os \}\}-\$\{\{ matrix\.arch \}\} --version', job)
    assert ran, "the built binary must run (`--version`) in the build container before it is uploaded"
    assert ran.start() < job.index("actions/upload-artifact"), "it must run BEFORE the upload"


def test_the_job_uses_the_containers_own_python_and_runs_as_root():
    """A container job has no sudo (it is root already), and setup-python would put the RUNNER's Python — built
    against the runner's glibc — back into the build."""
    job = _binaries_job()
    assert "actions/setup-python" not in job
    assert "sudo " not in job
