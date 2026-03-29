"""Tests for gateway /model command behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tok")}
    )
    runner.adapters = {Platform.TELEGRAM: MagicMock(send=AsyncMock())}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._evict_cached_agent = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_handle_message_dispatches_model_command():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner._handle_model_command = AsyncMock(return_value="model-ok")

    result = await runner._handle_message(_make_event("/model"))

    assert result == "model-ok"
    runner._handle_model_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_command_lists_current_and_configured_models():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        model="glm-5.1",
        provider="zai",
        base_url="https://api.z.ai/api/coding/paas/v4",
    )
    runner = _make_runner(session_entry)

    with patch(
        "hermes_cli.models.list_available_providers",
        return_value=[
            {"id": "zai", "label": "Z.AI", "authenticated": True, "aliases": []},
            {"id": "openai-codex", "label": "OpenAI Codex", "authenticated": True, "aliases": []},
            {"id": "openrouter", "label": "OpenRouter", "authenticated": False, "aliases": []},
        ],
    ), patch(
        "hermes_cli.models.curated_models_for_provider",
        side_effect=lambda provider: [
            ("glm-5.1", "latest"),
            ("glm-5", "stable"),
        ] if provider == "zai" else [("gpt-5.4", "default")],
    ), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "provider": "zai",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "z-key",
        },
    ):
        result = await runner._handle_model_command(_make_event("/model"))

    assert "Current model" in result
    assert "glm-5.1" in result
    assert "Configured providers & models" in result
    assert "openai-codex" in result
    assert "Not configured" in result
    runner.session_store.get_or_create_session.assert_called_once()


@pytest.mark.asyncio
async def test_model_command_preserves_existing_token_totals_when_switching():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        model="glm-5.1",
        provider="zai",
        base_url="https://api.z.ai/api/coding/paas/v4",
        input_tokens=111,
        output_tokens=222,
        cache_read_tokens=333,
        cache_write_tokens=444,
        estimated_cost_usd=1.23,
        cost_status="estimated",
    )
    runner = _make_runner(session_entry)

    with patch(
        "hermes_cli.model_switch.switch_model",
        return_value=SimpleNamespace(
            success=True,
            new_model="gpt-5.4",
            target_provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            provider_label="OpenAI Codex",
            warning_message="",
        ),
    ), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value={
            "provider": "zai",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "api_key": "z-key",
        },
    ):
        await runner._handle_model_command(_make_event("/model openai-codex:gpt-5.4"))

    runner.session_store.update_session.assert_called_once_with(
        session_entry.session_key,
        input_tokens=111,
        output_tokens=222,
        cache_read_tokens=333,
        cache_write_tokens=444,
        estimated_cost_usd=1.23,
        cost_status="estimated",
        model="gpt-5.4",
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )
