"""The web interface for `grid train` — for people who will never open a terminal.

The CLI is for engineers. This is for the support manager who wants her ticket archive to draft
replies, and the sales manager who wants his lead history to triage the inbox. Same engine
underneath: a workspace holds the uploaded export, the generated config, and the prepared task
files, and a run is launched as an ordinary `grid train run` subprocess.

Three rules the interface keeps:
  · plain language — "examples", "what good looks like", "start using this", never "GRPO"
  · never hide a refusal — too little data or a missing trainer is stated on the page, with
    the specific next step, before anyone waits a night for nothing
  · nothing serves customers unless it beat the model already serving them (the eval gate)
"""
from __future__ import annotations


def build_app(token: str = ""):
    """The app. `token` gates every page — required when this is bound off loopback (access.py)."""
    from fastapi import FastAPI

    from . import access, routes

    app = FastAPI(title="grid train", docs_url=None, redoc_url=None)
    routes.register(app)
    access.install(app, token)
    return app
