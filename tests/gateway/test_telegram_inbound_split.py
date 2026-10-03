"""Inbound Telegram splits are measured in UTF-16, not Python len().

Telegram clients split above 4096 UTF-16 code units. Astral chars (emoji)
cost two units each, so a maxed first chunk can have Python len() << 4000.
Using len() skips the near-split delay and dispatches the command before
the continuation arrives.
"""
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import utf16_len


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.05
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter.handle_message = AsyncMock()
    return adapter


def _make_update(text):
    msg = SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=12345, type="private", title=None, is_forum=False),
        from_user=SimpleNamespace(id=111, full_name="Test User", first_name="Test"),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )
    return SimpleNamespace(update_id=1, message=msg, effective_message=None)


def _emoji_near_split_command():
    # ASCII prefix + enough emoji that UTF-16 >= 4000 while Python len < 4000.
    emoji = "\U0001F600"  # 1 Python char, 2 UTF-16 units
    text = "/queue " + emoji * 2000
    assert len(text) < 4000
    assert utf16_len(text) >= 4000
    return text


@pytest.mark.asyncio
async def test_emoji_near_utf16_limit_command_is_batched_not_dispatched():
    adapter = _make_adapter()
    await adapter._handle_command(_make_update(_emoji_near_split_command()), SimpleNamespace())
    adapter.handle_message.assert_not_awaited()
    assert len(adapter._pending_text_batches) == 1


@pytest.mark.asyncio
async def test_emoji_chunk_stores_utf16_last_chunk_len():
    adapter = _make_adapter()
    text = "\U0001F600" * 2000
    assert len(text) == 2000
    assert utf16_len(text) == 4000
    await adapter._handle_text_message(_make_update(text), SimpleNamespace())
    pending = next(iter(adapter._pending_text_batches.values()))
    assert pending._last_chunk_len == 4000


def test_handle_command_uses_inbound_split_helper():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    src = inspect.getsource(TelegramAdapter._handle_command)
    assert "is_near_split" in src, (
        "Keep inbound UTF-16 split detection in inbound_split.is_near_split; "
        "do not compare len(event.text) against _SPLIT_THRESHOLD in adapter.py."
    )
