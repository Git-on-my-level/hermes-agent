"""Final-reply reconciliation for the commentary preview bubble.

One logical job that wakes the agent several times (background notices, goal
wakeups) used to scatter 2-3 Telegram bubbles per wake: the preview stack
stayed orphaned while the final went out as a fresh send. These contracts pin
the reconciliation: the final folds into the live preview bubble (stack kept,
answer appended), and every fallback hands delivery back to the caller.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _consumer(adapter, *, mode: str = "preview"):
    cfg_kwargs = {
        "edit_interval": 0.01,
        "buffer_threshold": 1,
        "commentary_mode": mode,
    }
    if mode == "preview":
        cfg_kwargs["commentary_waiting_label"] = "Waiting for zai/glm-5.3-flash/high..."
    return GatewayStreamConsumer(
        adapter, "chat", StreamConsumerConfig(**cfg_kwargs), metadata={"thread_id": "5181"},
    )


async def _feed_note(consumer, note="progress note"):
    task = asyncio_task = None
    import asyncio

    task = asyncio.get_running_loop().create_task(consumer.run())
    await asyncio.to_thread(consumer.on_commentary, note)
    await asyncio.sleep(0.15)
    return task


@pytest.mark.asyncio
async def test_final_folds_into_preview_bubble_keeping_stack():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    delivered = await consumer.reconcile_preview_with_final("All done: merged.")

    assert delivered is True
    assert consumer.final_response_sent is True
    # Exactly one interim send; the final was an EDIT, not a fresh bubble.
    adapter.send.assert_awaited_once()
    body = adapter.edit_message.await_args_list[-1].kwargs["content"]
    assert "progress note" in body
    assert "All done: merged." in body
    assert "───────" in body
    task.cancel()


@pytest.mark.asyncio
async def test_final_fresh_send_stands_when_answer_alone_exceeds_bubble():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 200
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    delivered = await consumer.reconcile_preview_with_final("x" * 300)

    assert delivered is False
    assert consumer.final_response_sent is False
    task.cancel()


@pytest.mark.asyncio
async def test_final_fresh_send_stands_when_reconciliation_edit_fails():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=False, error="boom"))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    delivered = await consumer.reconcile_preview_with_final("the answer")

    assert delivered is False
    assert consumer.final_response_sent is False
    task.cancel()


@pytest.mark.asyncio
async def test_reconciliation_noop_in_separate_mode():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    consumer = _consumer(adapter, mode="separate")

    delivered = await consumer.reconcile_preview_with_final("answer")

    assert delivered is False
    adapter.send.assert_not_awaited()
    adapter.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_reconciliation_does_not_edit_twice():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    first = await consumer.reconcile_preview_with_final("the answer")
    second = await consumer.reconcile_preview_with_final("the answer")

    assert first is True
    assert second is True  # already-delivered short-circuit, still suppresses a re-send
    reconcile_edits = [
        call for call in adapter.edit_message.await_args_list
        if "the answer" in call.kwargs.get("content", "")
    ]
    assert len(reconcile_edits) == 1
    task.cancel()
