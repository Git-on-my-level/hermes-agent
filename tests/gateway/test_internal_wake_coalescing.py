"""Drain-time coalescing of internal follow-up backlogs, and internal follow-ups not charging
the interrupt recursion cap (#816 regression class).

Incident shape: a session with a huge context babysits CI via several background watchers.
Every watcher completion enqueues one internal FIFO event; a long turn drained them one turn
each, re-sending the full context per event just for the model to answer another silence
marker, and the internal recursions consumed the interrupt-depth cap (3), stranding the rest
of the queue behind "Interrupt recursion depth 3 reached".

Contract (post codex-gpt-6.1-sol review):
- Coalescing merges COMPATIBLE internal events only (same plugin_injected flag and
  notification_category — text-only merging would drop delivery policy), in FIFO order
  (head → slot → overflow, promotion parity), stopping at the first incompatible item.
- After the slot is consumed and overflow remains, the overflow head re-stages into the
  slot so arrivals during the turn cannot jump the queue.
- Internal follow-ups are exempt from _MAX_INTERRUPT_DEPTH but bounded by their own
  _MAX_INTERNAL_DRAIN_DEPTH chain limit; the event re-queues when the bound hits.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.base import Platform
from gateway.session import SessionSource


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="supergroup",
        user_id="42", user_name="David", message_id="1",
    )


def _event(text: str, *, internal: bool, mid: str, plugin_injected: bool = False,
           metadata: dict = None) -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=_source(),
        message_id=mid, internal=internal, plugin_injected=plugin_injected,
        metadata=metadata if metadata is not None else {},
    )


class _Queue:
    """A real FIFO overflow list + pending slot (the adapter-facing stores)."""

    def __init__(self):
        self.overflow: list = []
        self.slot: dict = {}

    def overflow_queue(self, _key):
        return self.overflow


def _runner(queue: _Queue, head=None):
    """Runner bearing the REAL drain + coalescing + promotion methods and real queue plumbing."""
    from gateway.run import GatewayRunner
    from gateway.run_turn import GatewayTurnMixin

    adapter = MagicMock()
    adapter._pending_messages = queue.slot
    runner = SimpleNamespace(
        _overflow_queue=queue.overflow_queue,
        # REAL promotion semantics (stubbing this hid the review's ordering findings).
        _promote_queued_event=None,
    )
    runner._promote_queued_event = GatewayRunner._promote_queued_event.__get__(runner)
    runner._run_agent_drain_pending = GatewayTurnMixin._run_agent_drain_pending.__get__(runner)
    runner._coalesce_internal_followups = (
        GatewayTurnMixin._coalesce_internal_followups.__get__(runner)
    )
    runner._internal_events_batchable = GatewayTurnMixin._internal_events_batchable
    runner._pending_event_audio_paths = lambda pending_event: []
    runner._transcribe_and_echo_pending_voice = AsyncMock(return_value=("", ""))
    runner._draining = False
    runner._status_action_label = lambda: "test"
    if head is not None:
        adapter.get_pending_message = MagicMock(return_value=head)
    runner._adapter = adapter
    return runner


def _enqueue(runner, *events):
    """Seed the FOLLOWER events exactly as production holds them at drain time.

    ``head`` is the event the drain dequeues from the pending slot (stubbed via
    get_pending_message), so the followers live in the FIFO overflow; promotion stages
    the first of them into the slot mid-drain (GatewayRunner._promote_queued_event).
    """
    runner._overflow_queue("sess").extend(events)


def _result(**kw):
    base = {"interrupted": False, "messages": []}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Coalescing: real queue, real promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_backlog_coalesces_in_fifo_order():
    head = _event("proc A done", internal=True, mid="m1")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, _event("proc B done", internal=True, mid="m2"))
    _enqueue(runner, _event("proc C done", internal=True, mid="m3"))

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "proc A done\n\nproc B done\n\nproc C done"
    assert q.overflow == [] and "sess" not in q.slot


@pytest.mark.asyncio
async def test_human_in_overflow_stops_coalescing_and_is_never_crossed():
    head = _event("proc A done", internal=True, mid="m1")
    human = _event("what's the status?", internal=False, mid="m2")
    internal_tail = _event("proc B done", internal=True, mid="m3")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, human)
    _enqueue(runner, internal_tail)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending == "proc A done"
    # The human blocks the merge: it was staged next by promotion, the internal behind it
    # keeps its own turn (no reordering, nothing absorbed).
    assert q.slot.get("sess") is human
    assert q.overflow == [internal_tail]


@pytest.mark.asyncio
async def test_human_in_slot_blocks_overflow_merge():
    """Review finding 1: the slot is the NEXT event; a human there must stop coalescing
    entirely — merging overflow internals would run them ahead of the human."""
    head = _event("proc A done", internal=True, mid="m1")
    human_next = _event("status?", internal=False, mid="m2")
    internal_tail = _event("proc B done", internal=True, mid="m3")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, human_next)
    _enqueue(runner, internal_tail)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending == "proc A done"
    # Both keep their turns, in order: the human stays staged next, the internal behind it.
    assert q.overflow == [internal_tail]
    assert q.slot.get("sess") is human_next


@pytest.mark.asyncio
async def test_slot_restaged_when_overflow_remains():
    """Review finding 2: a consumed slot + non-empty overflow must re-stage the overflow
    head, or an arrival during the turn lands in the slot and jumps the queue."""
    head = _event("proc A done", internal=True, mid="m1")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, _event("proc B done", internal=True, mid="m2"))
    _enqueue(runner, _event("proc C done", internal=True, mid="m3"))
    _enqueue(runner, _event("proc D done", internal=True, mid="m4"))

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    # A (head) + B (staged by promotion) + C + D (overflow loop) — every compatible
    # internal follower absorbed; queue left fully drained.
    assert pending == "proc A done\n\nproc B done\n\nproc C done\n\nproc D done"
    assert q.slot.get("sess") is None and q.overflow == []

    # Re-stage contract separately: an INCOMPATIBLE tail must be re-staged so arrivals
    # during the turn cannot jump it.


@pytest.mark.asyncio
async def test_incompatible_tail_restaged_into_slot():
    """Review finding 2: when coalescing stops at an incompatible event, the consumed slot
    must be re-staged from the overflow so arrivals during the turn cannot jump the queue."""
    head = _event("proc A done", internal=True, mid="m1")
    human = _event("status?", internal=False, mid="m2")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, _event("proc B done", internal=True, mid="m3"))
    _enqueue(runner, human)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    # B absorbed; promotion had staged the HUMAN into the slot mid-drain... but the human
    # is next after B: coalesce must NOT consume it, and it must remain staged next.
    assert pending == "proc A done\n\nproc B done"
    assert q.slot.get("sess") is human
    assert q.overflow == []

    # Now the pure re-stage case: compatible B, incompatible human BEHIND it in overflow.
    head2 = _event("proc X done", internal=True, mid="m5")
    human2 = _event("human again", internal=False, mid="m6")
    q2 = _Queue()
    runner2 = _runner(q2, head=head2)
    _enqueue(runner2, _event("proc Y done", internal=True, mid="m7"))
    _enqueue(runner2, _event("proc Z done", internal=True, mid="m8"))
    _enqueue(runner2, human2)

    pending_event2, pending2 = await runner2._run_agent_drain_pending(
        _result(), runner2._adapter, _source(), "sess"
    )

    # X + Y + Z absorbed; the human is re-staged from overflow into the slot so a
    # mid-turn arrival lands BEHIND it.
    assert pending2 == "proc X done\n\nproc Y done\n\nproc Z done"
    assert q2.slot.get("sess") is human2
    assert q2.overflow == []


@pytest.mark.asyncio
async def test_plugin_injected_event_is_never_absorbed():
    head = _event("proc A done", internal=True, mid="m1")
    plugin = _event("plugin injection", internal=True, mid="m2", plugin_injected=True)
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, plugin)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending == "proc A done"
    # The plugin event keeps its own turn: still queued (staged in the slot by promotion),
    # never absorbed into the batch text.
    assert q.overflow == [plugin] or q.slot.get("sess") is plugin


@pytest.mark.asyncio
async def test_diagnostic_category_mismatch_is_never_absorbed():
    head = _event("visible completion", internal=True, mid="m1", metadata={"x": 1})
    diagnostic = _event("diagnostic blip", internal=True, mid="m2",
                        metadata={"notification_category": "diagnostic"})
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, diagnostic)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending == "visible completion"
    # The diagnostic event keeps its own (muting-eligible) turn: still queued, never absorbed.
    assert q.overflow == [diagnostic] or q.slot.get("sess") is diagnostic


@pytest.mark.asyncio
async def test_non_internal_head_is_never_coalesced():
    head = _event("hello", internal=False, mid="m1")
    internal = _event("proc A done", internal=True, mid="m2")
    q = _Queue()
    runner = _runner(q, head=head)
    _enqueue(runner, internal)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "hello"
    # The internal follower keeps its own turn: still queued, never absorbed.
    assert q.overflow == [internal] or q.slot.get("sess") is internal


@pytest.mark.asyncio
async def test_empty_queue_leaves_head_untouched():
    head = _event("proc A done", internal=True, mid="m1")
    q = _Queue()
    runner = _runner(q, head=head)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending_event is head
    assert pending == "proc A done"


@pytest.mark.asyncio
async def test_draining_discards_only_the_dequeued_head_not_the_backlog():
    """Drain discard must not drop wakes absorbed by coalescing.

    `_coalesce_internal_followups` folds slot + overflow onto the dequeued head
    before the `_draining` guard. If that runs first, restart/shutdown throws
    the whole batch away instead of leaving later internals for queue-mode persist.
    """
    head = _event("proc A done", internal=True, mid="m1")
    follower_b = _event("proc B done", internal=True, mid="m2")
    follower_c = _event("proc C done", internal=True, mid="m3")
    q = _Queue()
    runner = _runner(q, head=head)
    runner._draining = True
    _enqueue(runner, follower_b)
    _enqueue(runner, follower_c)

    pending_event, pending = await runner._run_agent_drain_pending(
        _result(), runner._adapter, _source(), "sess"
    )

    assert pending_event is None and pending is None
    # Head discarded as the one event that would recurse; promotion staged B
    # into the slot and C stays in overflow for persist.
    assert q.slot.get("sess") is follower_b
    assert q.overflow == [follower_c]


# ---------------------------------------------------------------------------
# Interrupt depth + internal drain chain bound
# ---------------------------------------------------------------------------


def _followup_runner(depth: int, drain_depth: int = 0, overflow: list = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._MAX_INTERRUPT_DEPTH = 3
    runner._MAX_INTERNAL_DRAIN_DEPTH = 8
    if overflow is not None:
        runner._overflow_queue = lambda session_key: overflow
    runner._run_agent = AsyncMock(return_value={"final_response": "ok", "messages": []})
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value="agent:main:telegram:group:-1001:5")
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="wake")
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    source = _source()
    turn_ctx = SimpleNamespace(
        source=source, session_id="sid",
        session_key="agent:main:telegram:group:-1001:5",
        run_generation=1, _interrupt_depth=depth, _internal_drain_depth=drain_depth,
        history=[], _status_thread_metadata=None,
        context_prompt=None, result_holder=[None],
    )
    return runner, turn_ctx


async def _run_followup(runner, turn_ctx, pending_event, adapter=None):
    from gateway.run import GatewayRunner

    await GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=adapter, pending=pending_event.text,
        pending_event=pending_event, response="resp", result=_result(), stream_task=None,
    )


@pytest.mark.asyncio
async def test_internal_followup_at_interrupt_cap_still_recurses_same_depth():
    runner, turn_ctx = _followup_runner(depth=3)
    await _run_followup(runner, turn_ctx, _event("proc done", internal=True, mid="m9"))
    runner._run_agent.assert_awaited_once()
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["_interrupt_depth"] == 3
    assert kwargs["_internal_drain_depth"] == 1


@pytest.mark.asyncio
async def test_human_followup_still_advances_interrupt_depth_and_resets_drain():
    runner, turn_ctx = _followup_runner(depth=0, drain_depth=5)
    await _run_followup(runner, turn_ctx, _event("hello", internal=False, mid="m9"))
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["_interrupt_depth"] == 1
    assert kwargs["_internal_drain_depth"] == 0


@pytest.mark.asyncio
async def test_internal_drain_chain_bound_restores_slot_order_and_stops():
    """Review finding 4 + recheck finding 1: continuous internal arrivals must not recurse
    unboundedly, and the bound handoff must preserve FIFO order for later resumption."""
    q = _Queue()
    runner, turn_ctx = _followup_runner(depth=0, drain_depth=8, overflow=q.overflow)
    adapter = MagicMock()
    adapter._pending_messages = q.slot  # the real overflow list stays connected
    event = _event("older wake", internal=True, mid="m10")     # the bounded event
    successor = _event("newer wake", internal=True, mid="m11")  # staged in the slot meanwhile
    q.slot["agent:main:telegram:group:-1001:5"] = successor

    await _run_followup(runner, turn_ctx, event, adapter=adapter)

    # No recursion: the chain bound hit.
    runner._run_agent.assert_not_awaited()
    key = "agent:main:telegram:group:-1001:5"
    # The OLDER event is restored to the SLOT (it runs first on resumption); the staged
    # successor moved to the overflow HEAD, ahead of any remaining siblings.
    assert q.slot.get(key) is event
    assert q.overflow[0] is successor

    # Empty-slot variant: the event lands in the slot, nothing is lost to a detached list.
    q2 = _Queue()
    runner2, turn_ctx2 = _followup_runner(depth=0, drain_depth=8, overflow=q2.overflow)
    adapter2 = MagicMock()
    adapter2._pending_messages = q2.slot
    lone = _event("only wake", internal=True, mid="m12")
    await _run_followup(runner2, turn_ctx2, lone, adapter=adapter2)
    runner2._run_agent.assert_not_awaited()
    assert q2.slot.get(key) is lone and q2.overflow == []
