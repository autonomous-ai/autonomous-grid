"""Single source of truth for the package version.

The version is declared once, in ``pyproject.toml`` (which also names and stamps the
built wheel). We read it back from the installed package metadata so ``grid --version``
can never drift from the wheel — unlike the old hardcoded literal here, which lagged
``pyproject.toml`` at the v0.1.1 release and made that wheel print "0.1.0".
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("grid")
except PackageNotFoundError:  # source checkout with no installed "grid" dist
    __version__ = "0.0.0+dev"
