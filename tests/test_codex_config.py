"""Tests for the codex seat's CONFIG_TOML — the lockdown written to ~/.grid/seats/codex-cli/."""
import tomllib

from shared.agent.seats import codex


def test_config_carries_no_apps_section():
    """`[apps._default.tools]` is not a field codex knows — `codex --strict-config` rejects it on
    0.146.0, so writing it blocked nothing and only read as though it had. Codex ignores
    unrecognised keys silently without that flag, which is why it looked like it worked."""
    config = tomllib.loads(codex.CONFIG_TOML)
    assert "apps" not in config


def test_config_still_disables_shell_tool():
    """shell_tool has its own feature flag and must stay disabled."""
    config = tomllib.loads(codex.CONFIG_TOML)
    assert config.get("features", {}).get("shell_tool") is False


def test_config_is_valid_toml():
    """The CONFIG_TOML string must parse without error."""
    config = tomllib.loads(codex.CONFIG_TOML)
    assert isinstance(config, dict)
    assert config.get("approval_policy") == "untrusted"
    assert config.get("sandbox_mode") == "read-only"
