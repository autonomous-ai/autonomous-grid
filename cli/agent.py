"""`grid agent` commands: install the agents that drive chat with tools (Hermes, Codex)."""
from __future__ import annotations

import argparse


def cmd_agent_install(args: argparse.Namespace) -> int:
    if args.name == "hermes":
        from shared.agent import installer

        if installer.is_installed() and not args.force:
            if installer.acp_ready():
                print(f"Hermes is already installed -> {installer.hermes_bin()}")
                return 0
            # The binary is there, but not the part the Grid app drives it through — an install
            # made before we asked for the `[acp]` extra. Saying "already installed" and stopping
            # left the app with an agent that failed every chat turn and no way to repair it
            # short of `--force`, so finish the job instead.
            print("Hermes is installed without ACP support; completing the install ...")
        path = installer.install_hermes()
        print(f"Installed hermes -> {path}")
        return 0

    if args.name == "codex":
        from remote import task_codex
        from shared.agent import codex_installer

        if codex_installer.is_installed() and not args.force:
            installed = codex_installer.codex_bin()
            if task_codex.supports_distributed_goals(str(installed)):
                print(f"Codex is already installed -> {installed}")
                return 0
            # Grid once pinned Codex before native Goal resume existed. File existence therefore
            # cannot mean "installed" for this command: transparently replace that known-bad build
            # with the current hash-pinned release instead of requiring users to discover --force.
            print("Codex is installed but cannot resume distributed Goals; upgrading it ...")
        path = codex_installer.install_codex()
        print(f"Installed codex -> {path}")
        return 0

    raise SystemExit(f"Unknown agent {args.name!r}. Choose from 'hermes' or 'codex'.")


def cmd_agent_status(args: argparse.Namespace) -> int:
    from remote import task_codex
    from shared.agent import codex_installer, installer

    hermes = installer.is_installed()
    codex = codex_installer.is_installed()
    print(f"Hermes: {'installed' if hermes else 'not installed'} ({installer.hermes_bin()})")
    codex_state = "not installed"
    if codex:
        codex_state = ("installed" if task_codex.supports_distributed_goals(
            str(codex_installer.codex_bin())) else "installed; Goal upgrade required")
    print(f"Codex: {codex_state} ({codex_installer.codex_bin()})")
    return 0
