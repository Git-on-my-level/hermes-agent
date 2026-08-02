"""Focused contracts for editable interim-commentary previews."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _consumer(adapter, **kwargs):
    return GatewayStreamConsumer(
        adapter,
        "chat",
        StreamConsumerConfig(
            edit_interval=0.01,
            buffer_threshold=1,
            commentary_mode="preview",
        ),
        metadata={"thread_id": "topic-7"},
        initial_reply_to_id="reply-42",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_preview_first_send_then_repeated_edits_keep_one_message_id():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="preview-1")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="preview-1")
    )
    consumer = _consumer(adapter)

    consumer.on_commentary("Checking the repo.")
    consumer.on_commentary("Running targeted tests.")
    consumer.on_commentary("Reviewing the diff.")
    consumer.finish()
    await consumer.run()

    adapter.send.assert_awaited_once_with(
        chat_id="chat",
        content="Checking the repo.",
        reply_to="reply-42",
        metadata={
            "thread_id": "topic-7",
            "reply_to_message_id": "reply-42",
            "expect_edits": True,
        },
    )
    assert [call.kwargs["message_id"] for call in adapter.edit_message.await_args_list] == [
        "preview-1",
        "preview-1",
    ]
    assert all(call.kwargs["finalize"] is True for call in adapter.edit_message.await_args_list)
    assert consumer.commentary_preview_message_ids == ("preview-1",)
    assert consumer.already_sent is False
    assert consumer.final_response_sent is False


@pytest.mark.asyncio
async def test_preview_edit_failure_falls_back_and_tracks_every_breadcrumb():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        side_effect=[
            SimpleNamespace(success=True, message_id="preview-1"),
            SimpleNamespace(success=True, message_id="preview-2"),
        ]
    )
    adapter.edit_message = AsyncMock(
        side_effect=[
            SimpleNamespace(success=False, message_id="preview-1", error="not editable"),
            SimpleNamespace(success=True, message_id="preview-2"),
        ]
    )
    consumer = _consumer(adapter)

    consumer.on_commentary("First.")
    consumer.on_commentary("Second survives failed edit.")
    consumer.on_commentary("Third edits the fallback.")
    consumer.finish()
    await consumer.run()

    assert [call.kwargs["content"] for call in adapter.send.await_args_list] == [
        "First.",
        "Second survives failed edit.",
    ]
    assert adapter.edit_message.await_args_list[-1].kwargs["message_id"] == "preview-2"
    assert consumer.commentary_preview_message_ids == ("preview-1", "preview-2")
    assert consumer.already_sent is False


@pytest.mark.asyncio
async def test_preview_edit_exception_falls_back_to_fresh_editable_message():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        side_effect=[
            SimpleNamespace(success=True, message_id="preview-1"),
            SimpleNamespace(success=True, message_id="preview-2"),
        ]
    )
    adapter.edit_message = AsyncMock(side_effect=RuntimeError("edit unavailable"))
    consumer = _consumer(adapter)

    consumer.on_commentary("First.")
    consumer.on_commentary("Second survives the exception.")
    consumer.finish()
    await consumer.run()

    assert [call.kwargs["content"] for call in adapter.send.await_args_list] == [
        "First.",
        "Second survives the exception.",
    ]
    assert consumer.commentary_preview_message_ids == ("preview-1", "preview-2")


@pytest.mark.asyncio
async def test_preview_tracks_all_adapter_result_ids_and_stops_ambiguous_edits():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        side_effect=[
            SimpleNamespace(
                success=True,
                message_id="preview-2",
                continuation_message_ids=("preview-1", "preview-2"),
            ),
            SimpleNamespace(success=True, message_id="preview-3"),
        ]
    )
    adapter.edit_message = AsyncMock()
    consumer = _consumer(adapter)

    consumer.on_commentary("First split by the adapter.")
    consumer.on_commentary("Second uses a fresh message.")
    consumer.finish()
    await consumer.run()

    assert adapter.edit_message.await_count == 0
    assert consumer.commentary_preview_message_ids == (
        "preview-2",
        "preview-1",
        "preview-3",
    )


@pytest.mark.asyncio
async def test_overlong_preview_uses_full_content_send_instead_of_clipping():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 140
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="separate-1")
    )
    adapter.edit_message = AsyncMock()
    consumer = _consumer(adapter)
    commentary = "A" * 80

    consumer.on_commentary(commentary)
    consumer.finish()
    await consumer.run()

    adapter.send.assert_awaited_once_with(
        chat_id="chat",
        content=commentary,
        reply_to=None,
        metadata={"thread_id": "topic-7"},
    )
    assert consumer.commentary_preview_message_ids == ()
    assert consumer.has_delivered_text(commentary) is False


@pytest.mark.asyncio
async def test_preview_equal_to_final_is_not_final_delivery_evidence():
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="preview-1")
    )
    consumer = _consumer(adapter)

    consumer.on_commentary("The final answer.")
    consumer.finish()
    await consumer.run()

    assert consumer.has_delivered_text("The final answer.") is False
    assert consumer.already_sent is False
    assert consumer.final_content_delivered is False
