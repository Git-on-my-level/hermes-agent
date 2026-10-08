from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.goals import CONTINUATION_PROMPT_TEMPLATE


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.callbacks = {}
        self._active_sessions = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SimpleNamespace(success=True)

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callbacks[session_key] = (generation, callback)


def _goal_continuation_event(source, goal="finish the task"):
    return MessageEvent(
        text=CONTINUATION_PROMPT_TEMPLATE.format(goal=goal),
        message_type=MessageType.TEXT,
        source=source,
    )


@pytest.mark.asyncio
async def test_goal_status_notice_defers_until_post_delivery_callback():
    """Regression: goal status must appear after the agent's visible reply.

    _post_turn_goal_continuation runs before BasePlatformAdapter sends the
    returned final response. It should therefore register a post-delivery
    callback, not send the judge status immediately.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        thread_id="thread-123",
        user_id="user-1",
    )

    await runner._defer_goal_status_notice_after_delivery(source, "✓ Goal achieved: done")

    assert adapter.calls == []
    assert len(adapter.callbacks) == 1

    _, callback = next(iter(adapter.callbacks.values()))
    result = callback()
    if hasattr(result, "__await__"):
        await result

    assert adapter.calls == [
        {
            "chat_id": "parent-channel",
            "content": "✓ Goal achieved: done",
            "reply_to": None,
            "metadata": {"thread_id": "thread-123"},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["important", "all", "off"])
@pytest.mark.parametrize("level", ["important", "info", "debug"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_goal_notice_modes_route_levels_and_notify(tmp_path, monkeypatch, mode, level, deferred, caplog):
    """Read the real YAML setting and route through direct or post-delivery sends."""
    import logging

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"goals:\n  notices: {mode}\n", encoding="utf-8")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", thread_id="thread", user_id="user")
    caplog.set_level(logging.INFO, logger="gateway.run")
    message = f"goal notice {mode}/{level}"
    send = runner._defer_goal_status_notice_after_delivery if deferred else runner._send_goal_status_notice
    await send(source, message, notice_level=level)
    expected_send = level == "important" or mode == "all" or (mode == "important" and level == "info")
    if deferred:
        assert adapter.calls == []
        assert bool(adapter.callbacks) == expected_send
        for _, callback in adapter.callbacks.values():
            await callback()
    assert len(adapter.calls) == int(expected_send)
    if expected_send:
        assert adapter.calls[0]["metadata"] == {"thread_id": "thread", **({"notify": True} if level == "important" else {})}
    else:
        assert message in caplog.text


@pytest.mark.asyncio
async def test_default_goal_notice_mode_is_important(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = {"goals": {"max_turns": 3}}
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user")
    await runner._send_goal_status_notice(source, "debug", notice_level="debug")
    await runner._send_goal_status_notice(source, "info", notice_level="info")
    await runner._send_goal_status_notice(source, "important", notice_level="important")
    assert [call["content"] for call in adapter.calls] == ["info", "important"]
    assert adapter.calls[0]["metadata"] == {}
    assert adapter.calls[1]["metadata"] == {"notify": True}
