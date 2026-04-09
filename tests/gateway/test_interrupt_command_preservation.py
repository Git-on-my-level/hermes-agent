"""Regression test: queued slash commands must stay commands after an interrupt.

Before this fix, `_run_agent()` pulled a queued `MessageEvent` from the adapter,
extracted only `pending_event.text`, and recursively called `_run_agent()` with the
plain string. That silently demoted `/new`, `/model`, etc. into normal chat text,
so Telegram topic commands looked unresponsive while a turn was active.
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


class _InterruptedAgent:
    """Fake agent that always returns an interrupted result."""

    def __init__(self, *args, **kwargs):
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        return {
            "interrupted": True,
            "messages": [],
            "final_response": "",
            "interrupt_message": None,
        }


class _AdapterWithQueuedCommand:
    def __init__(self, queued_event: MessageEvent, session_key: str):
        self._queued_event = queued_event
        self._active_sessions = {session_key: asyncio.Event()}

    def get_pending_message(self, session_key: str):
        event, self._queued_event = self._queued_event, None
        return event

    async def send(self, chat_id, text, **kwargs):
        return None


def _make_runner(handle_message_mock):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._background_tasks = set()
    runner._voice_mode = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._MAX_INTERRUPT_DEPTH = 4
    runner._tool_wrapper = None
    runner._status_tracker = None
    runner._progress_topics = {}
    runner._active_progress_tasks = {}
    runner._session_vars = {}
    runner._assistant_title = None
    runner._paired_sessions = {}
    runner._personality_overrides = {}
    runner._session_model_overrides = {}
    runner._session_model_settings = {}
    runner._session_provider_settings = {}
    runner._session_base_url_settings = {}
    runner._session_api_key_settings = {}
    runner._session_reasoning_settings = {}
    runner._session_show_reasoning_settings = {}
    runner._session_verbosity_settings = {}
    runner._approval_timeout_tasks = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._evict_cached_agent = lambda session_key: None
    runner._shutdown_gateway_honcho = lambda session_key: None
    runner._format_session_info = lambda entry: ""
    runner._is_user_authorized = lambda source: True
    runner._handle_message = handle_message_mock
    return runner


@pytest.mark.asyncio
async def test_interrupted_queued_command_is_redispatched_as_message_event(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _InterruptedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda **kw: {"provider": "openai", "api_mode": "responses", "api_key": "test-key"},
    )

    handle_message_mock = AsyncMock(return_value="reset ok")
    runner = _make_runner(handle_message_mock)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    session_key = "agent:main:telegram:dm:12345"
    queued_command = MessageEvent(
        text="/new",
        source=source,
        message_type=MessageType.COMMAND,
    )
    runner.adapters[Platform.TELEGRAM] = _AdapterWithQueuedCommand(queued_command, session_key)

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
    )

    assert result == "reset ok"
    handle_message_mock.assert_awaited_once()
    dispatched_event = handle_message_mock.await_args.args[0]
    assert isinstance(dispatched_event, MessageEvent)
    assert dispatched_event.is_command() is True
    assert dispatched_event.get_command() == "new"
