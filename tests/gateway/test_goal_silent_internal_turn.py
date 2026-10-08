"""A silent internal turn (process notice / plugin injection answered with [SILENT]) must not drive
/goal: judging the bare marker reads as not-waiting, clears a valid wait barrier, spends a turn,
posts a status line, and enqueues a continuation for a turn that deliberately said nothing."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner


def _runner():
    store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="sid")))
    return SimpleNamespace(
        async_session_store=store,
        _post_turn_goal_continuation=AsyncMock(),
        _post_turn_loop_completion=AsyncMock(),
        _final_text_for_post_turn_hooks=GatewayRunner._final_text_for_post_turn_hooks,
        _silent_internal_turn=GatewayRunner._silent_internal_turn,
    )


async def _run(runner, text, *, internal):
    await GatewayRunner._run_post_turn_hooks(
        runner, agent_result={"final_response": text}, source=object(), is_internal=internal,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["[SILENT]", " [SILENT] ", "NO_REPLY"])
async def test_silent_internal_turn_skips_goal_but_runs_loop_hook(text):
    runner = _runner()
    await _run(runner, text, internal=True)
    runner._post_turn_goal_continuation.assert_not_awaited()
    runner._post_turn_loop_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_silent_user_turn_still_drives_goal():
    runner = _runner()
    await _run(runner, "[SILENT]", internal=False)
    runner._post_turn_goal_continuation.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Still waiting on CI run 123; waker armed.", "[SILENT] but CI failed, fixing"])
async def test_internal_turn_with_content_still_drives_goal(text):
    runner = _runner()
    await _run(runner, text, internal=True)
    runner._post_turn_goal_continuation.assert_awaited_once()
