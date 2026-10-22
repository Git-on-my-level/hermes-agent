"""edit_message must follow send() onto a rebuilt Telegram adapter.

Disconnect sets _bot = None. send() delegates to the replacement adapter; a
bare success=False from edit_message is silent and makes commentary preview
emit a new bubble per item.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


def _dead_adapter():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._bot = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    return adapter


@pytest.mark.asyncio
async def test_edit_message_delegates_to_replacement_when_bot_is_gone():
    adapter = _dead_adapter()
    live = MagicMock()
    live.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="42"))
    adapter._replacement_telegram_adapter = lambda: live

    result = await adapter.edit_message("chat", "42", "stacked", finalize=True, metadata={"thread_id": "728"})

    assert result.success is True
    assert result.message_id == "42"
    live.edit_message.assert_awaited_once_with(
        "chat", "42", "stacked", finalize=True, metadata={"thread_id": "728"},
    )


@pytest.mark.asyncio
async def test_edit_message_without_replacement_stays_not_connected(monkeypatch):
    adapter = _dead_adapter()
    adapter._replacement_telegram_adapter = lambda: None

    async def _no_wait():
        return False

    monkeypatch.setattr(adapter, "_wait_for_reconnection", _no_wait)
    result = await adapter.edit_message("chat", "42", "stacked", finalize=True)
    assert result.success is False
    assert result.error == "Not connected"
