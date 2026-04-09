"""Regression tests for session-scoped gateway model/runtime wiring."""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


ZAI_MODEL = "z-ai/glm-5-turbo"
ZAI_PROVIDER = "zai"
ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
GLOBAL_MODEL = "gpt-5.4"
GLOBAL_PROVIDER = "openai-codex"
GLOBAL_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-10012345",
        chat_type="group",
        thread_id="17585",
        user_id="user-1",
        user_name="tester",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


class _CapturingAgent:
    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.model = kwargs.get("model")
        self.provider = kwargs.get("provider")
        self.base_url = kwargs.get("base_url")
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


class _InterruptThenCompleteAgent(_CapturingAgent):
    run_calls = 0

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        type(self).run_calls += 1
        if type(self).run_calls == 1:
            return {
                "final_response": "Operation interrupted.",
                "messages": [{"role": "user", "content": user_message}],
                "api_calls": 1,
                "interrupted": True,
                "interrupt_message": "follow-up",
            }
        return {
            "final_response": "ok",
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": "ok"},
            ],
            "api_calls": 1,
        }


def _make_run_agent_runner(config: GatewayConfig) -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


@pytest.mark.asyncio
async def test_run_agent_uses_session_model_override(monkeypatch, tmp_path):
    """A session /model selection must drive the agent creation with correct runtime."""
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    runner = _make_run_agent_runner(config)
    runner.session_store = MagicMock()
    runner._background_tasks = set()
    runner._async_flush_memories = AsyncMock()
    runner._shutdown_gateway_honcho = lambda _session_key: None
    runner._evict_cached_agent = MagicMock()
    runner._format_session_info = MagicMock(return_value="")
    runner._session_model_overrides = {}

    source = _make_source()
    session_key = "agent:main:telegram:group:-10012345:17585"

    # Set up a session model override (as /model command would)
    runner._session_model_overrides[session_key] = {
        "model": ZAI_MODEL,
        "provider": ZAI_PROVIDER,
        "api_key": "zai-key",
        "base_url": ZAI_BASE_URL,
        "api_mode": "chat_completions",
    }

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: GLOBAL_MODEL)

    runtime_requests = []

    def _fake_runtime_kwargs(*, requested=None, explicit_base_url=None):
        runtime_requests.append(
            {
                "requested": requested,
                "explicit_base_url": explicit_base_url,
            }
        )
        if requested == ZAI_PROVIDER:
            return {
                "provider": ZAI_PROVIDER,
                "api_mode": "chat_completions",
                "base_url": explicit_base_url or ZAI_BASE_URL,
                "api_key": "zai-key",
            }
        return {
            "provider": GLOBAL_PROVIDER,
            "api_mode": "codex_responses",
            "base_url": GLOBAL_BASE_URL,
            "api_key": "global-key",
        }

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _fake_runtime_kwargs)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    _CapturingAgent.last_init = None

    result = await runner._run_agent(
        message="What model are you?",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    assert _CapturingAgent.last_init["model"] == ZAI_MODEL
    assert _CapturingAgent.last_init["provider"] == ZAI_PROVIDER
    assert _CapturingAgent.last_init["base_url"] == ZAI_BASE_URL
    assert result["model"] == ZAI_MODEL
    assert result["provider"] == ZAI_PROVIDER
    assert result["base_url"] == ZAI_BASE_URL
    # The runtime resolver must have been called with the session-scoped provider
    assert any(
        r["requested"] == ZAI_PROVIDER and r["explicit_base_url"] == ZAI_BASE_URL
        for r in runtime_requests
    )


@pytest.mark.asyncio
async def test_run_agent_recursive_followup_keeps_session_runtime(monkeypatch, tmp_path):
    """Interrupted follow-up turns must keep the session-scoped runtime."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    runner = _make_run_agent_runner(config)
    runner.session_store = MagicMock()

    source = _make_source()
    session_key = "agent:main:telegram:group:-10012345:17585"

    # Session model override
    session_overrides = {
        "model": ZAI_MODEL,
        "provider": ZAI_PROVIDER,
        "api_key": "zai-key",
        "base_url": ZAI_BASE_URL,
        "api_mode": "chat_completions",
    }

    pending_event = _make_event("follow-up")
    adapter = MagicMock()
    adapter.has_pending_interrupt.return_value = False
    adapter.get_pending_message = MagicMock(side_effect=[pending_event, None])
    adapter._active_sessions = {session_key: asyncio.Event()}
    runner.adapters = {Platform.TELEGRAM: adapter}

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: GLOBAL_MODEL)

    runtime_requests = []

    def _fake_runtime_kwargs(*, requested=None, explicit_base_url=None):
        runtime_requests.append(
            {
                "requested": requested,
                "explicit_base_url": explicit_base_url,
            }
        )
        if requested == ZAI_PROVIDER:
            return {
                "provider": ZAI_PROVIDER,
                "api_mode": "chat_completions",
                "base_url": explicit_base_url or ZAI_BASE_URL,
                "api_key": "zai-key",
            }
        return {
            "provider": GLOBAL_PROVIDER,
            "api_mode": "codex_responses",
            "base_url": GLOBAL_BASE_URL,
            "api_key": "global-key",
        }

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _fake_runtime_kwargs)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _InterruptThenCompleteAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    _InterruptThenCompleteAgent.last_init = None
    _InterruptThenCompleteAgent.run_calls = 0

    result = await runner._run_agent(
        message="first turn",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-recursive",
        session_key=session_key,
        session_overrides=session_overrides,
    )

    assert result["final_response"] == "ok"
    assert _InterruptThenCompleteAgent.run_calls == 2
    # Both the initial and recursive calls should use the session-scoped runtime
    assert runtime_requests == [
        {
            "requested": ZAI_PROVIDER,
            "explicit_base_url": ZAI_BASE_URL,
        },
        {
            "requested": ZAI_PROVIDER,
            "explicit_base_url": ZAI_BASE_URL,
        },
    ]


@pytest.mark.asyncio
async def test_resolve_session_runtime_config_falls_back_to_global(monkeypatch):
    """Without session overrides, _resolve_session_runtime_config uses global config."""
    from gateway.run import _resolve_session_runtime_config

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: GLOBAL_MODEL)

    runtime_calls = []

    def _fake_runtime_kwargs(*, requested=None, explicit_base_url=None):
        runtime_calls.append({"requested": requested, "explicit_base_url": explicit_base_url})
        return {"provider": GLOBAL_PROVIDER, "api_key": "key"}

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", _fake_runtime_kwargs)

    model, runtime = _resolve_session_runtime_config(None)
    assert model == GLOBAL_MODEL
    assert runtime_calls == [{"requested": None, "explicit_base_url": None}]

    # With overrides
    runtime_calls.clear()
    model, runtime = _resolve_session_runtime_config({
        "model": ZAI_MODEL,
        "provider": ZAI_PROVIDER,
        "base_url": ZAI_BASE_URL,
    })
    assert model == ZAI_MODEL
    assert runtime_calls == [{"requested": ZAI_PROVIDER, "explicit_base_url": ZAI_BASE_URL}]
