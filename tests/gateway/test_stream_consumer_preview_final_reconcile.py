"""Preview-bubble retraction contracts (option 2: delete, never fold).

One logical job that wakes the agent several times (background notices, goal
wakeups) used to scatter 2-3 Telegram bubbles per wake: the preview stack
stayed orphaned beside the fresh final. The first fix folded the final into
the bubble — which leaked a literal ``[SILENT]`` marker whenever a watch-turn
went silent. These contracts pin the delete-semantics design: the final is
always a fresh send (or silent), the preview bubble is deleted, and a pure
silence marker never retracts nor writes anything.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from gateway.response_filters import is_intentional_silence_response
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
    task = asyncio.get_running_loop().create_task(consumer.run())
    await asyncio.to_thread(consumer.on_commentary, note)
    await asyncio.sleep(0.15)
    return task


def test_silence_marker_is_exactly_the_bare_sentinel():
    """The guard predicate: prose mentioning silence is delivered; bare marker is not."""
    assert is_intentional_silence_response("[SILENT]") is True
    assert is_intentional_silence_response("[SILENT] noting the fix landed") is False
    assert is_intentional_silence_response("All done: merged.") is False


@pytest.mark.asyncio
async def test_retract_deletes_the_preview_bubble_and_resets_state():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.delete_message = AsyncMock(return_value=SimpleNamespace(success=True))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    retracted = await consumer.retract_preview_bubbles()

    assert retracted is True
    adapter.delete_message.assert_awaited_once_with("chat", "preview-1")
    # State reset: a follow-up turn's consumer state never references a dead bubble.
    assert consumer._commentary_preview_message_id is None
    assert consumer._commentary_preview_entries == []
    task.cancel()


@pytest.mark.asyncio
async def test_silent_turn_keeps_progress_and_never_writes_the_marker():
    """A pure [SILENT] turn: no fold, no marker anywhere, progress bubble stays."""
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.delete_message = Mock()
    consumer = _consumer(adapter)
    task = await _feed_note(consumer, note="8 rounds in, thread count still oscillating...")

    final = "[SILENT]"
    # The gateway's guard: silence markers skip retraction entirely.
    if not is_intentional_silence_response(final):
        await consumer.retract_preview_bubbles()

    adapter.delete_message.assert_not_called()
    # No edit since the one interim send seeded the bubble: nothing folded the marker in.
    assert adapter.edit_message.await_count == 1
    # The bubble still shows the progress note, not the marker.
    assert consumer._commentary_preview_last_text == "8 rounds in, thread count still oscillating..."
    task.cancel()


@pytest.mark.asyncio
async def test_retract_survives_a_failed_delete():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.delete_message = AsyncMock(side_effect=[False, RuntimeError("refused")])
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    retracted = await consumer.retract_preview_bubbles()

    assert retracted is False  # caller's fresh send still fires
    assert adapter.delete_message.await_count == 2  # one retry, then give up
    task.cancel()


@pytest.mark.asyncio
async def test_retract_treats_message_not_found_as_success():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview-1"))
    adapter.delete_message = AsyncMock(side_effect=RuntimeError("Message to delete not found"))
    consumer = _consumer(adapter)
    task = await _feed_note(consumer)

    retracted = await consumer.retract_preview_bubbles()

    assert retracted is True  # already gone is the same end state
    task.cancel()


@pytest.mark.asyncio
async def test_retract_noop_in_separate_mode():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.delete_message = Mock()
    consumer = _consumer(adapter, mode="separate")

    retracted = await consumer.retract_preview_bubbles()

    assert retracted is True  # nothing to retract
    adapter.delete_message.assert_not_called()
