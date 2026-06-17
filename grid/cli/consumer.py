"""`grid consumer` commands: print OpenAI-compatible environment variables."""
from __future__ import annotations

import argparse

from .. import config, runtime


def cmd_consumer_env(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    print(f'export OPENAI_BASE_URL="{runtime.network_url(cfg)}/v1"')
    print('export OPENAI_API_KEY="local-lan"')
    return 0


