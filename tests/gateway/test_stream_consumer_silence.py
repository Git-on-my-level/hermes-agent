"""Streaming intentional-silence suppression.

When the agent chooses not to reply it emits a bare control marker
(``NO_REPLY`` / ``[SILENT]`` / …).  The gateway's whole-response filter
(``gateway/response_filters.is_intentional_silence_agent_result``) suppresses
this on the non-streaming delivery path, but the *streaming* path
(``GatewayStreamConsumer``) previously had no silence awareness: it edited the
raw marker onto the screen delta-by-delta and finalized it *before* the
whole-response filter could run.  On any streaming-capable adapter (Slack,
Telegram, Discord, …) users saw a literal ``NO_REPLY`` bubble.

These tests pin the two halves of the fix:

* ``is_partial_silence_marker`` — the mid-stream hold-back predicate.
* ``GatewayStreamConsumer`` — an exact-marker final buffer is suppressed and
  any already-shown preview is retracted, while substantive prose that merely
  mentions a marker is delivered normally.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.response_filters import (
    is_intentional_silence_response,
    is_partial_silence_marker,
)
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


# --------------------------------------------------------------------------
# is_partial_silence_marker — mid-stream hold-back predicate
# --------------------------------------------------------------------------

def test_partial_predicate_agrees_with_exact_on_full_markers():
    """Every exact silence marker is also a (trivial) partial of itself."""
    from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS

    for marker in LIVE_GATEWAY_SILENT_MARKERS:
        assert is_partial_silence_marker(marker) is True
        assert is_intentional_silence_response(marker) is True


# --------------------------------------------------------------------------
# GatewayStreamConsumer — end-to-end suppression through run()
# --------------------------------------------------------------------------

def _make_adapter(*, supports_delete: bool = True) -> MagicMock:
    """Minimal MagicMock adapter wired for send/edit/delete."""
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(
        success=True, message_id="preview_1",
    ))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(
        success=True, message_id="preview_1",
    ))
    if supports_delete:
        adapter.delete_message = AsyncMock(return_value=True)
    else:
        del adapter.delete_message  # type: ignore[attr-defined]
    return adapter


def _sent_and_edited(adapter):
    texts = []
    for call in adapter.send.call_args_list:
        texts.append(call.kwargs.get("content", ""))
    if getattr(adapter, "edit_message", None) is not None:
        for call in adapter.edit_message.call_args_list:
            texts.append(call.kwargs.get("content", ""))
    return texts


class TestStreamedSilenceSuppression:
    @pytest.mark.asyncio
    async def test_no_reply_only_stream_is_fully_suppressed(self):
        """A stream whose entire content is NO_REPLY sends nothing visible."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        consumer.on_delta("NO_REPLY")
        consumer.finish()
        await consumer.run()

        # No marker text ever reached the platform.
        for text in _sent_and_edited(adapter):
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"

        # Delivery flags stay False so the gateway does not treat the marker
        # as a delivered reply (its whole-response filter then drops it too).
        assert consumer.final_response_sent is False
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_partial_marker_preview_is_retracted(self):
        """A marker flushed mid-stream as a preview is deleted on completion."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        # Force a mid-stream preview: pretend "NO_REPLY" was already put on
        # screen (the pre-fix behaviour) before got_done runs.
        consumer._message_id = "preview_1"
        consumer._preview_message_ids = {"preview_1"}
        consumer._already_sent = True

        consumer.on_delta("NO_REPLY")
        consumer.finish()
        await consumer.run()

        # The stale preview was best-effort deleted.
        adapter.delete_message.assert_awaited_once_with("chat_1", "preview_1")
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_plugin_injected_note_is_held_and_then_suppressed(self):
        """A plugin-injected turn holds partial text until a trailing marker can be judged."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
            quiet_until_final=True, session_key="agent:main:telegram:dm:1",
        )
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Checked: CI still running.\n\n")
        await asyncio.sleep(0.3)
        assert all("CI still running" not in text for text in _sent_and_edited(adapter))
        consumer.on_delta("[SILENT]")
        consumer.finish()
        await task
        assert all("CI still running" not in text and "[SILENT]" not in text
                   for text in _sent_and_edited(adapter))
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_user_note_plus_marker_is_streamed(self):
        """A real user turn keeps the exact rule, so the same note is delivered."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.0, buffer_threshold=1, cursor=""),
        )
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Checked: CI still running.\n\n[SILENT]")
        consumer.finish()
        await task
        assert any("CI still running" in text for text in _sent_and_edited(adapter))


def _streaming_turn(plugin_injected: bool):
    """A turn with interim commentary and text streaming enabled."""
    from gateway.config import StreamingConfig
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    ctx = TurnContext(
        internal=True, plugin_injected=plugin_injected,
        interim_assistant_messages_enabled=True, user_config={},
        resolve_display_setting=lambda *_args: None,
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="chat_1"),
        session_key="agent:main:telegram:dm:1", _run_still_current=lambda: True,
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig(enabled=True)),
        _delivery_adapter_for=lambda _source: MagicMock(SUPPORTS_MESSAGE_EDITING=True),
        _build_stream_consumer_config=lambda *_a, **_k: (StreamConsumerConfig(), None),
    )
    return TurnRunner(runner, ctx)._setup_stream_consumer("telegram")


def test_internal_notification_keeps_interim_messages():
    """A process notice or goal wakeup is internal and still streams its work."""
    _consumer, delta_cb, interim_cb, want_interim = _streaming_turn(plugin_injected=False)
    agent = SimpleNamespace()
    agent.interim_assistant_callback = interim_cb if want_interim else None
    assert want_interim is True
    assert agent.interim_assistant_callback is not None
    assert delta_cb is not None


def test_plugin_injected_turn_is_quiet_until_final():
    """A heartbeat injection holds the reply; interim commentary is not wired."""
    _consumer, delta_cb, interim_cb, want_interim = _streaming_turn(plugin_injected=True)
    agent = SimpleNamespace()
    agent.interim_assistant_callback = interim_cb if want_interim else None
    assert want_interim is False
    assert agent.interim_assistant_callback is None
    assert delta_cb is None


