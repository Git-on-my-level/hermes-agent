"""A silent internal turn (process notice / plugin injection answered with exactly [SILENT]) must not
drive /goal while the goal's wait barrier still holds: judging the bare marker reads as not-waiting,
clears a live barrier, spends a turn, posts a status line, and enqueues a continuation. Once the
barrier has lifted (the awaited process exited), the same silent turn must still be judged."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import GatewayRunner
from hermes_cli.goals import GoalManager, GoalState


def _hooks_runner():
    store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="sid")))
    return SimpleNamespace(
        async_session_store=store,
        _post_turn_goal_continuation=AsyncMock(),
        _post_turn_loop_completion=AsyncMock(),
        _final_text_for_post_turn_hooks=GatewayRunner._final_text_for_post_turn_hooks,
        _silent_internal_turn=GatewayRunner._silent_internal_turn,
    )


async def _run_hooks(runner, text, *, internal):
    await GatewayRunner._run_post_turn_hooks(
        runner, agent_result={"final_response": text}, source=object(), is_internal=internal,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "[SILENT]", " [SILENT] ", "NO_REPLY",
    "Checked: CI still running.\n\n[SILENT]",
    "[SILENT] but CI failed, fixing",
])
async def test_silent_internal_turn_is_flagged_quiet_and_loop_hook_still_runs(text):
    runner = _hooks_runner()
    await _run_hooks(runner, text, internal=True)
    assert runner._post_turn_goal_continuation.await_args.kwargs.get("quiet_internal") is True
    runner._post_turn_loop_completion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "internal"), [
    ("[SILENT]", False),
    ("Checked: CI still running.\n\n[SILENT]", False),
    ("Still waiting on CI run 123; waker armed.", True),
    ("the lane said [SILENT] mid-sentence and kept talking", True),
])
async def test_other_turns_are_not_quiet(text, internal):
    runner = _hooks_runner()
    await _run_hooks(runner, text, internal=internal)
    assert "quiet_internal" not in runner._post_turn_goal_continuation.await_args.kwargs


def _goal_runner(mgr):
    async def _executor(fn):
        return fn()

    return SimpleNamespace(
        _post_turn_manager=AsyncMock(return_value=mgr),
        _turn_is_user_authored=lambda event: False,
        _run_in_executor_with_context=_executor,
        _goal_max_turns_from_config=lambda: 30,
    )


def _mgr(barrier_live: bool):
    mgr = MagicMock()
    mgr.has_goal.return_value = True
    mgr.is_active.return_value = True
    mgr.wait_barrier_live.return_value = barrier_live
    mgr.evaluate_after_turn.return_value = {"message": "", "continuation_prompt": "", "should_continue": False}
    return mgr


async def _run_goal_hook(mgr, quiet):
    await GatewayRunner._post_turn_goal_continuation(
        _goal_runner(mgr), session_entry=SimpleNamespace(session_id="sid"), source=None,
        final_response="[SILENT]", quiet_internal=quiet,
    )


@pytest.mark.asyncio
async def test_quiet_turn_with_live_barrier_skips_the_judge():
    mgr = _mgr(barrier_live=True)
    await _run_goal_hook(mgr, quiet=True)
    mgr.evaluate_after_turn.assert_not_called()


@pytest.mark.asyncio
async def test_quiet_turn_after_the_barrier_lifted_is_still_judged():
    mgr = _mgr(barrier_live=False)
    await _run_goal_hook(mgr, quiet=True)
    mgr.evaluate_after_turn.assert_called_once()


@pytest.mark.asyncio
async def test_non_quiet_turn_is_judged_even_with_live_barrier():
    mgr = _mgr(barrier_live=True)
    await _run_goal_hook(mgr, quiet=False)
    mgr.evaluate_after_turn.assert_called_once()
    mgr.wait_barrier_live.assert_not_called()


def _manager_with(state: GoalState) -> GoalManager:
    mgr = GoalManager(session_id="sid-barrier")
    mgr._state = state
    return mgr


def test_wait_barrier_live_is_read_only_and_ignores_the_age_cap():
    import time
    alive = _manager_with(GoalState(goal="g", waiting_on_pid=os.getpid(), waiting_since=time.time() - 86400))
    assert alive.wait_barrier_live() is True
    assert alive._state.waiting_on_pid == os.getpid()  # not cleared
    dead = _manager_with(GoalState(goal="g", waiting_on_pid=2**22 + 12345))
    assert dead.wait_barrier_live() is False
    assert _manager_with(GoalState(goal="g", waiting_until=time.time() + 60)).wait_barrier_live() is True
    assert _manager_with(GoalState(goal="g", waiting_until=time.time() - 1)).wait_barrier_live() is False
    assert _manager_with(GoalState(goal="g")).wait_barrier_live() is False
    assert _manager_with(GoalState(goal="g", status="paused", waiting_on_pid=os.getpid())).wait_barrier_live() is False
