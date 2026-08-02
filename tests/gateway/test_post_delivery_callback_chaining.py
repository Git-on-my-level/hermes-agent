"""Tests for ``BasePlatformAdapter.register_post_delivery_callback`` chaining.

When two features want to run after the final response lands on the same
session (e.g. background-review release + temporary-progress cleanup), the
registration API chains them rather than clobbering. Per-callback
exceptions are swallowed so one bad callback can't sabotage the others.
Stale-generation registrations are rejected.

The chained wrapper is ``async`` so it transparently supports sync or async
callbacks — the outer invoker in ``_handle_message`` awaits awaitable
callbacks, and a sync wrapper would silently drop coroutine results from
async callbacks chained behind it.
"""
import asyncio
import inspect

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _MinAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.fixture
def adapter():
    return _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)


def _invoke(cb):
    """Invoke a popped callback, awaiting if it returns a coroutine.

    Single-registration callbacks are returned as the raw user callable
    (sync). Chained callbacks (two or more registrations on the same
    session) are wrapped in an async helper. Tests use this helper so
    they don't have to care which case they're exercising.
    """
    result = cb()
    if inspect.isawaitable(result):
        asyncio.run(result)


class TestPostDeliveryCallbackChaining:
    def test_single_callback_fires(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A"]

    def test_two_callbacks_chain_in_order(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        adapter.register_post_delivery_callback("s", lambda: fired.append("B"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B"]

    def test_three_callbacks_chain_in_order(self, adapter):
        """Chain composes over an already-chained callback."""
        fired = []
        for label in ("A", "B", "C"):
            adapter.register_post_delivery_callback(
                "s", lambda x=label: fired.append(x)
            )
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B", "C"]


class TestPostDeliveryCallbackAsyncChaining:
    """When an async callback is chained, the wrapper must await it.

    Regression test for a bug where the sync ``_chained`` wrapper called
    async callbacks without awaiting, silently dropping the returned
    coroutine. This broke ``/goal`` continuations (Discord etc.) where
    the continuation injection is an async ``_deliver()`` coroutine.
    """

    def test_async_callback_in_chain_is_awaited(self, adapter):
        fired = []

        async def async_cb():
            await asyncio.sleep(0)
            fired.append("async")

        adapter.register_post_delivery_callback("s", lambda: fired.append("sync"))
        adapter.register_post_delivery_callback("s", async_cb)
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["sync", "async"]


class _DeliveryOutcomeAdapter(_MinAdapter):
    def __init__(self, *, succeeds: bool):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.succeeds = succeeds

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=self.succeeds, message_id="final-1" if self.succeeds else None)


def _event():
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm"),
        message_id="user-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False])
async def test_success_only_callback_follows_final_delivery_ack(succeeds):
    adapter = _DeliveryOutcomeAdapter(succeeds=succeeds)
    fired = []

    async def handler(event):
        return "final answer"

    adapter.set_message_handler(handler)
    adapter.register_post_successful_delivery_callback(
        "session",
        lambda: fired.append("cleanup"),
    )

    await adapter._process_message_background(_event(), "session")

    assert fired == (["cleanup"] if succeeds else [])
    assert "session" not in adapter._post_successful_delivery_callbacks


@pytest.mark.asyncio
async def test_success_only_callback_is_discarded_on_cancellation():
    adapter = _DeliveryOutcomeAdapter(succeeds=True)
    fired = []

    async def handler(event):
        raise asyncio.CancelledError

    adapter.set_message_handler(handler)
    adapter.register_post_successful_delivery_callback(
        "session",
        lambda: fired.append("cleanup"),
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter._process_message_background(_event(), "session")

    assert fired == []
    assert "session" not in adapter._post_successful_delivery_callbacks


@pytest.mark.asyncio
async def test_cancelled_turn_retains_created_commentary_preview():
    adapter = _DeliveryOutcomeAdapter(succeeds=True)
    adapter.deleted = []

    async def delete_message(chat_id, message_id):
        adapter.deleted.append((chat_id, message_id))
        return True

    adapter.delete_message = delete_message

    async def handler(event):
        consumer = GatewayStreamConsumer(
            adapter,
            event.source.chat_id,
            StreamConsumerConfig(commentary_mode="preview"),
            initial_reply_to_id=event.message_id,
        )
        consumer.on_commentary("Work was interrupted.")
        consumer.finish()
        await consumer.run()
        preview_ids = consumer.commentary_preview_message_ids

        async def cleanup():
            for message_id in preview_ids:
                await adapter.delete_message(event.source.chat_id, message_id)

        adapter.register_post_successful_delivery_callback("session", cleanup)
        raise asyncio.CancelledError

    adapter.set_message_handler(handler)

    with pytest.raises(asyncio.CancelledError):
        await adapter._process_message_background(_event(), "session")

    assert adapter.deleted == []
    assert "session" not in adapter._post_successful_delivery_callbacks
