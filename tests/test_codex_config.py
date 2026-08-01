"""Tests for the codex seat's CONFIG_TOML — the lockdown written to ~/.grid/seats/codex-cli/."""
import tomllib

from shared.agent.seats import codex


def test_config_blocks_apply_patch():
    """apply_patch has no [features] flag (unlike shell_tool), so it must be blocked via the
    [apps] config — 'blocked by app configuration' is cleaner than the generic JSON-RPC error."""
    config = tomllib.loads(codex.CONFIG_TOML)
    tools = config.get("apps", {}).get("_default", {}).get("tools", {})
    assert tools.get("apply_patch", {}).get("enabled") is False


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
