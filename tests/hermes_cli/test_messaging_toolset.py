"""Behavioral coverage for the default-off outbound messaging toolset."""

from hermes_cli.tools_config import _get_platform_tools


def test_default_telegram_excludes_messaging():
    enabled = _get_platform_tools({}, "telegram", include_default_mcp_servers=False)

    assert "messaging" not in enabled


def test_explicit_telegram_messaging_opt_in_includes_messaging():
    enabled = _get_platform_tools(
        {"platform_toolsets": {"telegram": ["hermes-telegram", "messaging"]}},
        "telegram",
        include_default_mcp_servers=False,
    )

    assert "messaging" in enabled


def test_removing_telegram_messaging_opt_in_excludes_messaging():
    enabled = _get_platform_tools(
        {"platform_toolsets": {"telegram": ["hermes-telegram"]}},
        "telegram",
        include_default_mcp_servers=False,
    )

    assert "messaging" not in enabled
