"""Entry point for the Nuitka standalone build.

Nuitka compiles a *script file* into the binary, so this mirrors ``cli/__main__.py``
(``python -m cli``): it calls ``cli.main`` and exits with its return code, so the frozen
``grid`` binary dispatches exactly like the installed console script — including the hidden
internal subcommands (``__server`` / ``__engine`` / ``__remote-engine``) the CLI re-execs
itself with. Kept tiny and import-light on purpose.
"""
from cli import main

if __name__ == "__main__":
    raise SystemExit(main())
