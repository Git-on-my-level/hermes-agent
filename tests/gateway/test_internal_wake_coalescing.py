"""Drain-time coalescing of the all-internal follow-up backlog, and internal follow-ups not
charging the interrupt recursion cap (#816 regression class).

Incident shape: a session with a huge context babysits CI via several background watchers.
Every watcher completion enqueues one internal FIFO event; a long turn drained them one turn
each, re-sending the full context per event just for the model to answer another silence
marker, and the internal recursions consumed the interrupt-depth cap (3), stranding the rest
of the queue behind "Interrupt recursion depth 3 reached".
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.base import Platform


def _source():
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="supergroup",
        user_id="42", user_name="David", message_id="1",
    )


def _event(text: str, *, internal: bool, mid: str) -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=_source(),
        message_id=mid, internal=internal,
    )


def _runner(overflow: list, adapter=None, head=None) -> SimpleNamespace:
    """Minimal runner bearing the real drain + coalescing methods and stub queue plumbing."""
    from gateway.run_turn import GatewayTurnMixin

    if adapter is None:
        adapter = MagicMock()
        adapter._pending_messages = {}
    if head is not None:
        adapter.get_pending_message = MagicMock(return_value=head)
    runner = SimpleNamespace(
        _overflow_queue=lambda session_key: overflow,
        _promote_queued_event=lambda session_key, adapter, pending_event: pending_event,
        # No media on any stub event: drain skips the voice-transcription branch.
        _pending_event_audio_paths=lambda pending_event: [],
        _transcribe_and_echo_pending_voice=AsyncMock(return_value=("", "")),
        _draining=False,
        _status_action_label=lambda: "test",
    )
    runner._run_agent_drain_pending = GatewayTurnMixin._run_agent_drain_pending.__get__(runner)
    runner._coalesce_internal_followups = (
        GatewayTurnMixin._coalesce_internal_followups.__get__(runner)
    )
    runner._stub_adapter = adapter
    return runner


def _result(**kw):
    base = {"interrupted": False, "messages": []}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_backlog_coalesces_into_one_event():
    head = _event("proc A done", internal=True, mid="m1")
    second = _event("proc B done", internal=True, mid="m2")
    third = _event("proc C done", internal=True, mid="m3")
    adapter = MagicMock()
    adapter._pending_messages = {}
    runner = _runner([second, third], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "proc A done\n\nproc B done\n\nproc C done"
    # All three internal events were consumed in this single turn.
    assert runner._overflow_queue("sess") == []


@pytest.mark.asyncio
async def test_human_event_in_overflow_stops_coalescing():
    head = _event("proc A done", internal=True, mid="m1")
    human = _event("what's the status?", internal=False, mid="m2")
    adapter = MagicMock()
    adapter._pending_messages = {}
    runner = _runner([human], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "proc A done"
    # The human event was NOT consumed: it keeps its own turn, in arrival order.
    assert runner._overflow_queue("sess") == [human]


@pytest.mark.asyncio
async def test_non_internal_head_is_never_coalesced():
    head = _event("hello", internal=False, mid="m1")
    internal = _event("proc A done", internal=True, mid="m2")
    adapter = MagicMock()
    adapter._pending_messages = {}
    runner = _runner([internal], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "hello"
    assert runner._overflow_queue("sess") == [internal]


@pytest.mark.asyncio
async def test_pending_slot_next_head_joins_before_overflow_tail():
    """After promotion the slot holds the NEXT event (promoted overflow head), so the merge
    order is head -> slot -> overflow to keep FIFO arrival order."""
    head = _event("proc A done", internal=True, mid="m1")
    promoted_next = _event("proc B done", internal=True, mid="m2")
    tail = _event("proc C done", internal=True, mid="m3")
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter.get_pending_message = MagicMock(return_value=head)
    runner = _runner([promoted_next, tail], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending == "proc A done\n\nproc B done\n\nproc C done"
    # Everything was consumed in this single turn.
    assert runner._overflow_queue("sess") == []
    assert "sess" not in adapter._pending_messages


@pytest.mark.asyncio
async def test_slot_only_internal_event_joins_head():
    """No promotion (empty overflow): the slot event is the true next event and joins after
    the head."""
    head = _event("proc A done", internal=True, mid="m1")
    adapter = MagicMock()
    adapter._pending_messages = {"sess": _event("proc B done", internal=True, mid="m2")}
    runner = _runner([], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending == "proc A done\n\nproc B done"
    assert "sess" not in adapter._pending_messages


@pytest.mark.asyncio
async def test_human_event_in_slot_stops_slot_merge():
    head = _event("proc A done", internal=True, mid="m1")
    adapter = MagicMock()
    human_slot = _event("are you there?", internal=False, mid="m2")
    adapter._pending_messages = {"sess": human_slot}
    runner = _runner([], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending == "proc A done"
    assert adapter._pending_messages["sess"] is human_slot


@pytest.mark.asyncio
async def test_empty_overflow_leaves_head_untouched():
    head = _event("proc A done", internal=True, mid="m1")
    adapter = MagicMock()
    adapter._pending_messages = {}
    runner = _runner([], adapter=adapter, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "proc A done"


# ---------------------------------------------------------------------------
# Interrupt depth
# ---------------------------------------------------------------------------


def _followup_runner(depth: int):
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    runner._MAX_INTERRUPT_DEPTH = 3
    runner._run_agent = AsyncMock(return_value={"final_response": "ok", "messages": []})
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="agent:main:telegram:group:-1001:5")
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="wake")
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="supergroup", user_id="42",
    )
    turn_ctx = SimpleNamespace(
        source=source, session_id="sid",
        session_key="agent:main:telegram:group:-1001:5",
        run_generation=1, _interrupt_depth=depth, history=[], _status_thread_metadata=None,
        context_prompt=None, result_holder=[None],
    )
    return runner, turn_ctx


@pytest.mark.asyncio
async def test_internal_followup_at_cap_still_recurses_without_deepening():
    runner, turn_ctx = _followup_runner(depth=3)
    pending_event = _event("proc done", internal=True, mid="m9")

    await GatewayRunnerQF(runner, turn_ctx, pending_event)

    # Depth was already at the cap, but an internal drain is not a new interrupt: it recursed
    # anyway, with the SAME depth.
    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["_interrupt_depth"] == 3


@pytest.mark.asyncio
async def test_human_followup_still_advances_interrupt_depth():
    runner, turn_ctx = _followup_runner(depth=0)
    pending_event = _event("hello", internal=False, mid="m9")

    await GatewayRunnerQF(runner, turn_ctx, pending_event)

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["_interrupt_depth"] == 1


async def GatewayRunnerQF(runner, turn_ctx, pending_event):
    from gateway.run import GatewayRunner

    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending=pending_event.text,
        pending_event=pending_event, response="resp", result=_result(), stream_task=None,
    )
