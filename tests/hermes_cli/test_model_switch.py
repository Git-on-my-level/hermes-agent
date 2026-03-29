"""Regression tests for explicit provider switching behavior."""

from unittest.mock import patch

from hermes_cli.model_switch import switch_model


def test_switch_model_slash_provider_syntax_switches_to_zai_without_autodetect():
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "provider": "zai",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "glm-key",
        },
    ), patch(
        "hermes_cli.models.validate_requested_model",
        return_value={"accepted": True, "persist": True, "recognized": True, "message": None},
    ):
        result = switch_model(
            "zai/glm-5.1",
            current_provider="openai-codex",
            current_base_url="https://chatgpt.com/backend-api/codex",
        )

    assert result.success is True
    assert result.target_provider == "zai"
    assert result.new_model == "glm-5.1"
    assert result.warning_message == ""


def test_switch_model_explicit_provider_keeps_current_provider_without_autodetect():
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "codex-token",
        },
    ), patch(
        "hermes_cli.models.validate_requested_model",
        return_value={"accepted": True, "persist": True, "recognized": True, "message": None},
    ):
        result = switch_model(
            "openai-codex:gpt-5.4-mini",
            current_provider="openai-codex",
            current_base_url="https://chatgpt.com/backend-api/codex",
        )

    assert result.success is True
    assert result.target_provider == "openai-codex"
    assert result.new_model == "gpt-5.4-mini"
    assert result.warning_message == ""