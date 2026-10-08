"""Tests for gateway /goal verdict-message delivery.

The judge verdict message ("✓ Goal achieved", "⏸ budget exhausted", etc.)
must reach the user after each turn. Before this fix the code checked
``hasattr(adapter, "send_message")`` — but adapters expose ``send()``,
never ``send_message``, so the check always evaluated False and users
never saw verdicts. This test locks in the fix.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    # Pre-warm the SessionDB cache from this SYNC context. The tests call
    # GoalManager.set() on the event-loop thread, where _get_session_db()
    # refuses to construct SessionDB inline (loop-liveness guard) and only
    # waits _DB_BOOTSTRAP_LOOP_WAIT_S for a background bootstrap. On a loaded
    # CI runner the init overruns that window, the goal write is silently
    # dropped by design, and the continuation path no-ops — the recurring
    # sends == [] flake. Warming here uses the direct construction path, so
    # the loop-thread set() always finds a cached DB.
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    """Minimal adapter that records send() invocations."""

    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner_with_adapter(session_id: str = None):
    from gateway.run import GatewayRunner
    import uuid

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    # Default to a unique session_id so parallel runs
    # don't see each other's GoalManager state (DEFAULT_DB_PATH gets frozen at
    # module-import time, defeating per-test HERMES_HOME monkeypatches).
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


async def _drain_until(condition, timeout=5.0):
    """Yield to the event loop until ``condition()`` is truthy (bounded).

    The goal-continuation path finishes its sends/enqueues on spawned tasks;
    a fixed 0.05s sleep raced them on loaded CI runners (#88975). Returns as
    soon as the condition holds — the asserts after the call stay exact.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_goal_verdict_continue_enqueues_continuation(hermes_home):
    """When the judge says continue, progress is quiet and the
    continuation-prompt event must be delivered. The continuation prompt is
    routed through the adapter's pending-messages FIFO so the goal loop
    proceeds on the next turn."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("polish the docs")

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "still needs work", False, None, False)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="here's a partial edit",
        )
        await _drain_until(lambda: adapter._pending_messages)

    assert adapter.sends == []
    # Continuation prompt enqueued for next turn
    assert adapter._pending_messages, "continuation prompt must be enqueued in pending_messages"


@pytest.mark.asyncio
async def test_goal_verdict_budget_exhausted_sends_pause(hermes_home):
    """When the budget is exhausted, a '⏸ Goal paused' message must be sent
    and no further continuation enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager, save_goal

    mgr = GoalManager(session_entry.session_id, default_max_turns=2)
    state = mgr.set("tiny goal", max_turns=2)
    state.turns_used = 2
    save_goal(session_entry.session_id, state)

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "keep going", False, None, False)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="still partial",
        )
        await _drain_until(lambda: adapter.sends)

    assert len(adapter.sends) == 1
    content = adapter.sends[0]["content"]
    assert "paused" in content.lower()
    assert "turns used" in content.lower()
    assert adapter.sends[0]["metadata"]["notify"] is True
    # No continuation enqueued when budget is exhausted
    assert not adapter._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["important", "all", "off"])
@pytest.mark.parametrize("verdict", ["done", "blocked", "wait", "gap"])
async def test_post_turn_goal_decision_carries_notice_level(hermes_home, mode, verdict):
    """Real goal persistence and post-turn routing honor the judge decision's severity."""
    from hermes_cli.goals import GoalManager

    (hermes_home / "config.yaml").write_text(f'goals:\n  notices: "{mode}"\n', encoding="utf-8")
    runner, adapter, entry, src = _make_runner_with_adapter()
    manager = GoalManager(entry.session_id)
    manager.set("Verify both hosts")
    level = {"done": "info", "blocked": "important", "wait": "debug", "gap": "debug"}[verdict]
    try:
        with patch("hermes_cli.goals.judge_goal", return_value=(
            verdict, "pusher needs verification", False, {"seconds": 60} if verdict == "wait" else None, False,
        )):
            await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="Done")
        expected_send = level == "important" or mode == "all" or (level == "info" and mode == "important")
        assert len(adapter.sends) == int(expected_send)
        if expected_send:
            assert adapter.sends[0]["metadata"].get("notify") is (True if level == "important" else None)
        if verdict == "gap":
            assert "ONE decision request" in next(iter(adapter._pending_messages.values())).text
            assert GoalManager(entry.session_id).is_active()
        else:
            assert not adapter._pending_messages
    finally:
        runner._shutdown_executor(drain_timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["important", "all", "off"])
async def test_inferred_goal_notice_is_info_and_shows_checklist(hermes_home, mode):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_cli.goals import GoalContract, GoalManager

    (hermes_home / "config.yaml").write_text(
        f'goals:\n  auto_infer: true\n  notices: "{mode}"\n', encoding="utf-8",
    )
    runner, adapter, entry, src = _make_runner_with_adapter()
    checklist = "listen at K=0; pusher at K=0; live env verified on both; KB closed"
    event = MessageEvent(text="verify both and close KB", message_type=MessageType.TEXT, source=src)
    try:
        with (
            patch("hermes_cli.goals.infer_goal_from_turn", return_value="Verify both and close KB"),
            patch("hermes_cli.goals.draft_contract", return_value=GoalContract(outcome=checklist)),
            patch("hermes_cli.goals.judge_goal", return_value=("wait", "rolls running", False, {"seconds": 60}, False)),
        ):
            await runner._post_turn_goal_continuation(
                session_entry=entry, source=src, final_response="I will verify both and close KB", event=event,
            )
        assert GoalManager(entry.session_id).state.source == "auto"
        inferred = [send for send in adapter.sends if "Goal inferred" in send["content"]]
        assert len(inferred) == (0 if mode == "off" else 1)
        if inferred:
            assert checklist in inferred[0]["content"]
            assert "notify" not in inferred[0]["metadata"]
        assert len(adapter.sends) == {"important": 1, "all": 2, "off": 0}[mode]
    finally:
        runner._shutdown_executor(drain_timeout=2)
