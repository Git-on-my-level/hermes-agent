"""E2E: a silent heartbeat into a parked goal flows through the real gateway path.

adapter.handle_message(internal event) -> GatewayRunner._handle_message -> (scripted agent reply)
-> real _run_post_turn_hooks -> real _post_turn_goal_continuation -> real GoalManager on a real
SessionDB, parked on a real child process. Only the LLM turn and the judge call are stubbed.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from hermes_cli import goals
from tests.e2e.conftest import make_adapter, make_runner, make_session_entry, make_source


def _judge_continue(*_a, **_kw):
    return "continue", "keep going", False, None, False


async def _settle(mock, calls: int, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if mock.await_count >= calls:
            await asyncio.sleep(0.3)  # let the post-turn hooks run after the turn returns
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"agent turn {calls} never ran")


@pytest.mark.asyncio
async def test_silent_heartbeat_keeps_live_park_and_judges_once_the_process_exits():
    source = make_source(Platform.TELEGRAM)
    entry = make_session_entry(Platform.TELEGRAM, source)
    runner = make_runner(Platform.TELEGRAM, entry)
    runner._run_post_turn_hooks = GatewayRunner._run_post_turn_hooks.__get__(runner)  # the code under test
    runner._handle_message_with_agent = AsyncMock(return_value={"final_response": "[SILENT]"})
    adapter = make_adapter(Platform.TELEGRAM, runner)

    mgr = goals.GoalManager(session_id=entry.session_id)
    mgr.set("ship the release")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        mgr.wait_on(child.pid, reason="CI watcher")
        mgr._state.waiting_since = time.time() - 3600  # a real heartbeat lands after the 30-min barrier cap
        mgr._save()
        judge = patch("hermes_cli.goals.judge_goal", side_effect=_judge_continue)
        with judge as judge_mock:
            heartbeat = MessageEvent(
                text="[Goal heartbeat 1/3 — no session activity for 50m; goal waiting on pid]",
                message_type=MessageType.TEXT, source=source, internal=True, allow_gateway_control=False,
            )
            await adapter.handle_message(heartbeat)
            await _settle(runner._handle_message_with_agent, 1)

            state = goals.load_goal(entry.session_id)
            assert judge_mock.call_count == 0, "a silent turn judged a goal whose wait still holds"
            assert state.status == "active" and state.waiting_on_pid == child.pid
            assert state.turns_used == 0
            sent = [str(c.args[1]) for c in adapter.send.await_args_list if len(c.args) > 1]
            assert not any("Goal" in s or "Continuing" in s for s in sent), sent

            child.kill()
            child.wait(timeout=10)
            await adapter.handle_message(MessageEvent(
                text="[Goal heartbeat 2/3 — no session activity for 50m; goal waiting on pid]",
                message_type=MessageType.TEXT, source=source, internal=True, allow_gateway_control=False,
            ))
            await _settle(runner._handle_message_with_agent, 2)
            assert judge_mock.call_count == 1, "a silent turn after the process exited was not judged"
            assert goals.load_goal(entry.session_id).waiting_on_pid is None
    finally:
        if child.poll() is None:
            child.kill()
