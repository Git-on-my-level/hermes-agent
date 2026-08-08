"""Gateway re-arms Telegram typing after mid-turn compaction completion."""

from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import TurnRunner


async def _drain_pending() -> None:
    await asyncio.sleep(0)
    pending = [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    # concurrent.futures done-callbacks may schedule more work
    await asyncio.sleep(0)
    pending = [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _install_schedule(monkeypatch):
    def _safe_schedule(coro, loop, logger=None, log_message=""):
        fut: concurrent.futures.Future = concurrent.futures.Future()

        async def _run():
            try:
                result = await coro
            except Exception as exc:
                fut.set_exception(exc)
            else:
                fut.set_result(result)

        asyncio.get_running_loop().create_task(_run())
        return fut

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "safe_schedule_threadsafe", _safe_schedule)
    return gateway_run


@pytest.mark.asyncio
async def test_status_callback_compacted_rearms_typing(monkeypatch):
    adapter = MagicMock()
    adapter.resume_typing_for_chat = MagicMock()
    adapter._telegram_typing_cooldown_until = {"chat-1": 999999.0}
    adapter.send_typing = AsyncMock()

    gateway_run = _install_schedule(monkeypatch)
    monkeypatch.setattr(
        gateway_run,
        "_prepare_gateway_status_message",
        lambda platform, event_type, message: message,
    )
    monkeypatch.setattr(
        gateway_run,
        "_send_or_update_status_coro",
        AsyncMock(return_value=SimpleNamespace(success=True, message_id="status-1")),
    )

    ctx = SimpleNamespace(
        _status_adapter=adapter,
        _status_chat_id="chat-1",
        _status_thread_metadata={"thread_id": "t1"},
        _loop_for_step=asyncio.get_running_loop(),
        _cleanup_progress=False,
        _cleanup_msg_ids=[],
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram")),
        _run_still_current=lambda: True,
    )
    runner = TurnRunner(MagicMock(), ctx)
    runner._status_callback_sync(
        "compacted", "✓ Context compaction complete — continuing turn..."
    )
    await _drain_pending()

    adapter.resume_typing_for_chat.assert_called_with("chat-1")
    assert "chat-1" not in adapter._telegram_typing_cooldown_until
    adapter.send_typing.assert_awaited()
    assert adapter.send_typing.await_args.kwargs.get("metadata") == {"thread_id": "t1"}


@pytest.mark.asyncio
async def test_compacted_rearm_runs_even_when_status_text_suppressed(monkeypatch):
    adapter = MagicMock()
    adapter.resume_typing_for_chat = MagicMock()
    adapter.send_typing = AsyncMock()

    gateway_run = _install_schedule(monkeypatch)
    monkeypatch.setattr(
        gateway_run,
        "_prepare_gateway_status_message",
        lambda *a, **k: None,  # suppressed
    )

    ctx = SimpleNamespace(
        _status_adapter=adapter,
        _status_chat_id="chat-9",
        _status_thread_metadata=None,
        _loop_for_step=asyncio.get_running_loop(),
        _cleanup_progress=False,
        _cleanup_msg_ids=[],
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram")),
        _run_still_current=lambda: True,
    )
    runner = TurnRunner(MagicMock(), ctx)
    runner._status_callback_sync(
        "compacted", "✓ Context compaction complete — continuing turn..."
    )
    await _drain_pending()

    adapter.resume_typing_for_chat.assert_called_with("chat-9")
    adapter.send_typing.assert_awaited()
