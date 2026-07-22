"""`grid agent` commands: install the agents that drive chat with tools (Hermes, Codex)."""
from __future__ import annotations

import argparse


def cmd_agent_install(args: argparse.Namespace) -> int:
    if args.name == "hermes":
        from shared.agent import installer

        if installer.is_installed() and not args.force:
            print(f"Hermes is already installed -> {installer.hermes_bin()}")
            return 0
        path = installer.install_hermes()
        print(f"Installed hermes -> {path}")
        return 0

    if args.name == "codex":
        from shared.agent import codex_installer

        if codex_installer.is_installed() and not args.force:
            print(f"Codex is already installed -> {codex_installer.codex_bin()}")
            return 0
        path = codex_installer.install_codex()
        print(f"Installed codex -> {path}")
        return 0

    raise SystemExit(f"Unknown agent {args.name!r}. Choose from 'hermes' or 'codex'.")


def cmd_agent_status(args: argparse.Namespace) -> int:
    from shared.agent import codex_installer, installer

    hermes = installer.is_installed()
    codex = codex_installer.is_installed()
    print(f"Hermes: {'installed' if hermes else 'not installed'} ({installer.hermes_bin()})")
    print(f"Codex: {'installed' if codex else 'not installed'} ({codex_installer.codex_bin()})")
    return 0
