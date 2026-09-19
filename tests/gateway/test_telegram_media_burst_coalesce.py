"""
Fork invariant: non-album Telegram media (voice/audio/video/document) coalesces through
the same burst batcher as photo bursts, so a multi-item paste sent while the agent is
busy arrives as ONE follow-up turn instead of N.

Upstream keeps each *already-separated* busy follow-up its own FIFO turn (#114363 /
ed1b34128b); this adapter-level debounce is what prevents the separation in the first
place for pastes that straddle the busy boundary. Error notes (oversize, unreadable,
cache failure) stay immediate via ``_dispatch_with_text`` and never join a burst.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import _event_media_is_stt_input
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.handle_message = AsyncMock()
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    return a


def _media_event(path: str, mime: str, message_type: MessageType, text: str = "") -> MessageEvent:
    from types import SimpleNamespace

    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="100", chat_type="dm", user_id="1", thread_id=None, profile=None,
    )
    return MessageEvent(
        text=text, message_type=message_type, source=source,
        media_urls=[path], media_types=[mime], message_id=f"m-{path}",
    )


async def _drain(adapter):
    tasks = [t for t in list(adapter._pending_photo_batch_tasks.values()) if not t.done()]
    if tasks:
        await asyncio.gather(*tasks)


class TestMediaBurstCoalesce:
    @pytest.mark.asyncio
    async def test_two_voice_notes_enqueued_back_to_back_are_one_turn(self, adapter):
        """The paste case: two voice items inside the burst window coalesce into ONE
        handle_message dispatch with both media paths (previously each dispatched
        immediately → two busy follow-up turns)."""
        adapter._enqueue_media_event(
            _media_event("/tmp/v1.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")
        adapter._enqueue_media_event(
            _media_event("/tmp/v2.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")

        await _drain(adapter)

        adapter.handle_message.assert_awaited_once()
        merged = adapter.handle_message.await_args.args[0]
        assert merged.media_urls == ["/tmp/v1.ogg", "/tmp/v2.ogg"]

    @pytest.mark.asyncio
    async def test_mixed_type_paste_coalesces_into_one_event(self, adapter):
        """Screenshots-style mix (image doc + voice) lands as a single multi-media turn."""
        adapter._enqueue_media_event(
            _media_event("/tmp/a.jpg", "image/jpeg", MessageType.PHOTO, text="shot"), "media-enqueue")
        adapter._enqueue_media_event(
            _media_event("/tmp/v1.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")

        await _drain(adapter)

        adapter.handle_message.assert_awaited_once()
        merged = adapter.handle_message.await_args.args[0]
        assert merged.media_urls == ["/tmp/a.jpg", "/tmp/v1.ogg"]
        assert "shot" in (merged.text or "")

    @pytest.mark.asyncio
    async def test_later_document_keeps_not_inlined_flag(self, adapter):
        """A following large text document must keep media_text_inlined=False so
        inbound notes do not claim its content was included below."""
        doc = _media_event("/tmp/notes.txt", "text/plain", MessageType.DOCUMENT)
        doc.media_text_inlined = [False]
        adapter._enqueue_media_event(
            _media_event("/tmp/a.jpg", "image/jpeg", MessageType.PHOTO, text="shot"),
            "media-enqueue")
        adapter._enqueue_media_event(doc, "document-enqueue")

        await _drain(adapter)

        merged = adapter.handle_message.await_args.args[0]
        assert merged.media_urls == ["/tmp/a.jpg", "/tmp/notes.txt"]
        assert merged.media_text_inlined == [None, False]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_type", [MessageType.DOCUMENT, MessageType.AUDIO])
    async def test_voice_followup_promotes_type_so_stt_is_not_skipped(self, adapter, first_type):
        """DOCUMENT/AUDIO veto automatic STT for the whole event; a mixed paste
        that then adds a voice note must promote the surviving type to VOICE."""
        first_mime = "text/plain" if first_type == MessageType.DOCUMENT else "audio/mpeg"
        adapter._enqueue_media_event(
            _media_event("/tmp/first.bin", first_mime, first_type), "media-enqueue")
        adapter._enqueue_media_event(
            _media_event("/tmp/v1.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")

        await _drain(adapter)

        merged = adapter.handle_message.await_args.args[0]
        assert merged.message_type == MessageType.VOICE
        assert _event_media_is_stt_input(merged, 1) is True

    @pytest.mark.asyncio
    async def test_events_outside_the_window_stay_separate_turns(self, adapter):
        """Nothing is lost across a quiet gap: two items spaced beyond the burst window
        dispatch as two turns, in arrival order."""
        adapter._media_batch_delay_seconds = 0.05
        adapter._enqueue_media_event(
            _media_event("/tmp/v1.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")
        await _drain(adapter)
        adapter._enqueue_media_event(
            _media_event("/tmp/v2.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")
        await _drain(adapter)

        assert adapter.handle_message.await_count == 2
        assert adapter.handle_message.await_args_list[0].args[0].media_urls == ["/tmp/v1.ogg"]
        assert adapter.handle_message.await_args_list[1].args[0].media_urls == ["/tmp/v2.ogg"]

    @pytest.mark.asyncio
    async def test_held_while_disconnected_never_dispatches(self, adapter):
        """Same hold contract as photo/text batches: media bursts enqueued during a
        disconnect are held for redispatch, never flushed into a torn-down session."""
        adapter._mark_disconnected()
        adapter._enqueue_media_event(
            _media_event("/tmp/v1.ogg", "audio/ogg", MessageType.VOICE), "media-enqueue")

        await _drain(adapter)

        adapter.handle_message.assert_not_awaited()
        assert any(e.media_urls == ["/tmp/v1.ogg"] for e in adapter._held_inbound_events)
