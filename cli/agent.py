"""`grid agent` commands: install the agent that drives chat with tools (Hermes)."""
from __future__ import annotations

import argparse


def cmd_agent_install(args: argparse.Namespace) -> int:
    from shared.agent import installer

    if args.name != "hermes":
        raise SystemExit(f"Unknown agent {args.name!r}. Only 'hermes' is supported.")

    if installer.is_installed() and not args.force:
        print(f"Hermes is already installed -> {installer.hermes_bin()}")
        return 0

    path = installer.install_hermes()
    print(f"Installed hermes -> {path}")
    return 0


def cmd_agent_status(args: argparse.Namespace) -> int:
    from shared.agent import installer

    installed = installer.is_installed()
    print(f"Hermes: {'installed' if installed else 'not installed'} ({installer.hermes_bin()})")
    return 0
