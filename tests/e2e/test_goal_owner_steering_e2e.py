"""E2E: the user's mid-goal message reaches the judge through the real gateway path; internal turns don't.

adapter.handle_message -> GatewayRunner._handle_message -> (scripted agent reply) -> real
_run_post_turn_hooks -> real _post_turn_goal_continuation -> real GoalManager.evaluate_after_turn ->
real judge_goal prompt. Only the LLM turn and the judge's HTTP call are stubbed.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from hermes_cli import goals
from tests.e2e.conftest import make_adapter, make_runner, make_session_entry, make_source

MERGE_GO = "You can merge on CI green and after all reviews"


async def _settle(mock, calls: int, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if mock.await_count >= calls:
            await asyncio.sleep(0.3)  # let the post-turn hooks run after the turn returns
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"agent turn {calls} never ran")


@pytest.mark.asyncio
async def test_user_message_mid_goal_is_judge_steering_and_continuations_are_not():
    source = make_source(Platform.TELEGRAM)
    entry = make_session_entry(Platform.TELEGRAM, source)
    runner = make_runner(Platform.TELEGRAM, entry)
    runner._run_post_turn_hooks = GatewayRunner._run_post_turn_hooks.__get__(runner)  # the code under test
    runner._handle_message_with_agent = AsyncMock(return_value="Merged PR #20647; CI green.")
    adapter = make_adapter(Platform.TELEGRAM, runner)

    goals.GoalManager(session_id=entry.session_id).set(
        "Push the fixes to PR #20647 and report",
        contract=goals.GoalContract(outcome="fixes pushed; report sent", verification="CI green",
                                    constraints="Do not merge the PR"),
    )
    prompts = []

    def judge(call_llm, system, prompt, timeout):
        prompts.append(prompt)
        verdict = "continue" if len(prompts) == 1 else "done"  # one goal continuation, then stop
        return json.dumps({"verdict": verdict, "reason": "r"})

    with patch("hermes_cli.goals._call_goal_judge_llm", side_effect=judge):
        await adapter.handle_message(MessageEvent(text=MERGE_GO, message_type=MessageType.TEXT, source=source))
        await _settle(runner._handle_message_with_agent, 2)  # the user's turn + the goal continuation

    assert len(prompts) == 2
    assert MERGE_GO in prompts[0] and "newer message wins" in prompts[0]
    assert MERGE_GO in prompts[1], "steering must persist across the goal's own continuation turns"
    state = goals.load_goal(entry.session_id)
    assert [m["text"] for m in state.owner_messages] == [MERGE_GO], "a continuation was recorded as steering"
    assert state.status == "done"
