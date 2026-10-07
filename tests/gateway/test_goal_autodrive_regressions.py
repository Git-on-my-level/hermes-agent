"""Enabled gateway regressions for upstream goal ports #114921 and #134448."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


SID = "autodrive-regression"
KEY = "autodrive-key"


def event(text, *, internal=False):
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, internal=internal,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="user", chat_type="dm"),
    )


class SessionStore:
    def get_or_create_session(self, source, **kwargs):
        return SimpleNamespace(session_id=SID)

    def _generate_session_key(self, source):
        return KEY


@pytest.fixture
def enabled_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "goals:\n  auto_start: true\n  auto_infer: true\n  max_turns: 6\n", encoding="utf-8",
    )
    goals._DB_CACHE.clear()
    goals._get_session_db()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")})
    runner.session_store = SessionStore()
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}
    runner._draining = False
    runner._session_state(KEY)
    monkeypatch.setattr(runner, "_session_key_for_source", lambda source: KEY)
    monkeypatch.setattr(runner, "_defer_goal_status_notice_after_delivery", AsyncMock())
    yield runner, adapter
    runner._shutdown_executor(drain_timeout=2)
    goals._DB_CACHE.clear()


def queued(runner, adapter):
    first = adapter._pending_messages.get(KEY)
    return ([first] if first is not None else []) + list(runner._overflow_queue(KEY) or [])


def seed_automatic_goal(objective="Ship the original release"):
    manager = goals.GoalManager(SID, default_max_turns=6)
    state = manager.set_inferred(objective)
    return manager, state


def install_busy_turn(runner, monkeypatch):
    agent = SimpleNamespace(steer=Mock(return_value=True))
    runner._session_state(KEY).turn.agent = agent
    monkeypatch.setattr(runner, "_fold_into_running_turn", lambda *args: None)
    monkeypatch.setattr(runner, "_effective_busy_input_mode", lambda source: "steer")
    monkeypatch.setattr(runner, "_effective_busy_text_mode", lambda source: "interrupt")
    monkeypatch.setattr(runner, "_hm_busy_slash_or_photo", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(runner, "_hm_busy_telegram_grace_queue", lambda *args: False)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda source: True)
    monkeypatch.setattr(runner, "_admit_bot_message_for_source", lambda source: True)
    monkeypatch.setattr(runner, "_route_plaintext_approval_while_busy", AsyncMock(return_value=False))
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["runner", "adapter"])
async def test_goal_busy_correction_replaces_automatic_objective(enabled_gateway, monkeypatch, route):
    runner, adapter = enabled_gateway
    manager, _ = seed_automatic_goal()
    old = runner._synthetic_prompt_event(event("start").source, manager.next_continuation_prompt())
    runner._enqueue_fifo(KEY, old, adapter)
    real = event("Keep this queued user message")
    runner._enqueue_fifo(KEY, real, adapter)
    agent = install_busy_turn(runner, monkeypatch)
    correction = event("Ship the corrected release instead")
    if route == "runner":
        await runner._hm_handle_running_session_message(correction, correction.source, KEY)
    else:
        await runner._handle_active_session_busy_message(correction, KEY)
    state = goals.GoalManager(SID).state
    assert state.goal == correction.text
    assert state.source == "auto_start"
    assert state.turns_used == 0
    assert queued(runner, adapter) == [real]
    agent.steer.assert_called_once()
    # A busy message may also traverse the cold path when dequeued: no second reset.
    state.turns_used = 1
    goals.save_goal(SID, state)
    await runner._auto_start_goal_for_inbound_event(correction)
    assert goals.GoalManager(SID).state.turns_used == 1


@pytest.mark.asyncio
async def test_goal_auto_start_replacement_clears_old_continuations(enabled_gateway):
    runner, adapter = enabled_gateway
    manager, state = seed_automatic_goal()
    state.source = "auto_start"
    goals.save_goal(SID, state)
    old = runner._synthetic_prompt_event(event("start").source, manager.next_continuation_prompt())
    runner._enqueue_fifo(KEY, old, adapter)
    await runner._auto_start_goal_for_inbound_event(event("Publish the corrected report"))
    assert goals.GoalManager(SID).state.goal == "Publish the corrected report"
    assert queued(runner, adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pointer", [False, True])
async def test_goal_kickoff_is_synthetic_and_preserves_contract(enabled_gateway, pointer):
    runner, adapter = enabled_gateway
    objective = "Ship the signed release" if not pointer else "Ship the signed release and verify all artifacts. " * 12
    db = goals._get_session_db()
    db.ensure_session(SID)
    db.append_message(SID, "user", objective)
    command = event(f"/goal {objective}\nverify: CI and artifact checks pass")
    await runner._handle_goal_command(command)
    manager = goals.GoalManager(SID)
    manager.add_subgoal("Check release notes")
    manager.add_gate("echo verified")
    before = manager.state.to_json()
    kickoff = adapter._pending_messages[KEY]
    if pointer:
        assert kickoff.text == goals.GOAL_ALREADY_SEEN_KICK
    assert not runner._turn_is_user_authored(kickoff)
    await runner._auto_start_goal_for_inbound_event(kickoff)
    assert goals.GoalManager(SID).state.to_json() == before


@pytest.mark.asyncio
async def test_goal_gate_continuation_preserves_budget_and_pause_dequeues_it(enabled_gateway, monkeypatch):
    runner, adapter = enabled_gateway
    manager, state = seed_automatic_goal()
    state.source = "auto_start"
    goals.save_goal(SID, state)
    manager.add_gate("verify-quality", max_retries=4)
    manager.state.max_turns = 2
    goals.save_goal(SID, manager.state)
    monkeypatch.setattr(goals, "run_gate", Mock(return_value=(False, 1, "quality failed")))
    decision = manager.evaluate_after_turn("done")
    gate_event = event(decision["continuation_prompt"])
    assert runner._is_goal_continuation_event(gate_event)
    assert not runner._turn_is_user_authored(gate_event)
    await runner._auto_start_goal_for_inbound_event(gate_event)
    state = goals.GoalManager(SID).state
    assert (state.goal, state.source, state.turns_used) == (manager.state.goal, "auto_start", 1)
    assert len(state.gates) == 1 and state.gates[0].attempts == 1
    runner._enqueue_fifo(KEY, gate_event, adapter)
    await runner._run_post_turn_hooks(
        agent_result="Repair attempted", source=gate_event.source, is_internal=False, event=gate_event,
    )
    state = goals.GoalManager(SID).state
    assert (state.turns_used, state.max_turns, state.status) == (2, 2, "paused")
    assert state.gates[0].attempts == 2
    assert queued(runner, adapter) == []
    # Pause/clear must recognize this variant in both the pending slot and overflow.
    runner._enqueue_fifo(KEY, gate_event, adapter)
    real = event("Keep this user message")
    runner._enqueue_fifo(KEY, real, adapter)
    runner._enqueue_fifo(KEY, gate_event, adapter)
    await runner._handle_goal_command(event("/goal pause"))
    assert goals.GoalManager(SID).state.status == "paused"
    assert queued(runner, adapter) == [real]


@pytest.mark.asyncio
async def test_goal_enabled_lifecycle_preserves_state_and_single_continuation(enabled_gateway, monkeypatch):
    runner, adapter = enabled_gateway
    infer = Mock(return_value="Ship the inferred release after CI passes")
    monkeypatch.setattr(goals, "infer_goal_from_turn", infer)
    monkeypatch.setattr(goals, "draft_contract", lambda goal: goals.GoalContract(outcome=goal, verification="CI passes"))
    judge = Mock(return_value=("continue", "unfinished", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)

    async def finish(turn, response="still working"):
        await runner._auto_start_goal_for_inbound_event(turn)
        await runner._run_post_turn_hooks(
            agent_result=response, source=turn.source, is_internal=turn.internal, event=turn,
        )

    def check(objective, source, turns, status="active", count=1):
        state = goals.GoalManager(SID).state
        assert (state.goal, state.source, state.turns_used, state.status) == (objective, source, turns, status)
        pending = queued(runner, adapter)
        assert len(pending) == count
        assert all(runner._is_goal_continuation_event(item) and objective in item.text for item in pending)

    manager = goals.GoalManager(SID, default_max_turns=6)
    assert goals.maybe_infer_goal(manager, "ship it", "I'll ship after CI passes")
    objective = manager.state.goal
    check(objective, "auto", 0, count=0)
    await runner._run_post_turn_hooks(
        agent_result="I'll ship after CI passes", source=event("ship it").source,
        is_internal=False, event=event("ship it"),
    )
    check(objective, "auto", 1)
    await finish(event("Background command exited successfully", internal=True))
    check(objective, "auto", 2)
    continuation = adapter._pending_messages.pop(KEY)
    await finish(continuation)
    check(objective, "auto", 3)
    install_busy_turn(runner, monkeypatch)
    correction = event("Ship the corrected release after the new CI run")
    await runner._hm_handle_running_session_message(correction, correction.source, KEY)
    check(correction.text, "auto_start", 0, count=0)
    await runner._run_post_turn_hooks(
        agent_result="Running the corrected checks", source=correction.source,
        is_internal=False, event=correction,
    )
    check(correction.text, "auto_start", 1)
    judge.return_value = ("blocked", "need signing authorization", False, None, False)
    continuation = adapter._pending_messages.pop(KEY)
    await finish(continuation, "Need signing authorization")
    check(correction.text, "auto_start", 2, status="paused", count=0)
    await finish(event("Signing authorization is available"))
    check(correction.text, "auto_start", 2, status="paused", count=0)
    infer.assert_called_once()
