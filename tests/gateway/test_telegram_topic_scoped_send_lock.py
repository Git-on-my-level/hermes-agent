"""Fork keep-list: the #114396 per-chat send lock is scoped to chat+topic, not chat alone.

Upstream keys the FIFO send lock by chat id, so in a forum group every topic's sends queue
behind every other topic's in-flight send (a media upload, a chunked final): the waiting
placeholder of a topic the user just switched to lands seconds late. Topic ids partition the
conversation views the interleave bug can appear in, so the lock is keyed by chat+topic —
same-stream ordering (upstream's fix) is preserved while cross-topic sends run in parallel.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_send_to_a_new_topic_is_not_queued_behind_another_topics_in_flight_send():
    """Topic 222's send must complete while topic 111's send is still blocked mid-API-call.

    Under the upstream chat-wide key the second send waits on the chat lock and this times out —
    that wait is the late 'Waiting for...' placeholder David saw after the 2026-09-19 sync."""
    adapter = _adapter()
    order: list = []
    gate = asyncio.Event()
    release = asyncio.Event()

    async def fake_send_message(text: str, **_kw):
        tag = text.split()[0]
        order.append(("start", tag))
        if tag == "TOPICA":
            gate.set()
            await release.wait()  # topic A's send is stuck in the API call
        order.append(("done", tag))
        return MagicMock(message_id=len(order))

    adapter._bot.send_message = fake_send_message

    first = asyncio.create_task(
        adapter.send("chat", "TOPICA long media upload", metadata={"thread_id": "111"}))
    await asyncio.wait_for(gate.wait(), timeout=3)

    second = asyncio.create_task(
        adapter.send("chat", "TOPICB Waiting for model...", metadata={"thread_id": "222"}))
    done, _ = await asyncio.wait({second}, timeout=2)

    assert second in done, f"cross-topic send queued behind another topic's in-flight send: {order}"
    release.set()
    await first
    # B started and finished while A was still blocked mid-API-call; A finished last.
    assert order == [("start", "TOPICA"), ("start", "TOPICB"), ("done", "TOPICB"), ("done", "TOPICA")], order


@pytest.mark.asyncio
async def test_general_topic_and_no_metadata_share_one_fifo():
    """Telegram's General topic IS thread 1 and General replies often arrive with no thread
    metadata — both render in the same view, so both must serialize on the bare chat key."""
    adapter = _adapter()
    order: list = []
    gate = asyncio.Event()
    release = asyncio.Event()

    async def fake_send_message(text: str, **_kw):
        tag = text.split()[0]
        order.append(("start", tag))
        if tag == "FIRST":
            gate.set()
            await release.wait()
        order.append(("done", tag))
        return MagicMock(message_id=len(order))

    adapter._bot.send_message = fake_send_message
    first = asyncio.create_task(adapter.send("chat", "FIRST general", metadata={"thread_id": "1"}))
    await asyncio.wait_for(gate.wait(), timeout=3)
    second = asyncio.create_task(adapter.send("chat", "SECOND anchored"))  # no thread metadata
    done, _ = await asyncio.wait({second}, timeout=2)

    assert second not in done, f"General topic and no-metadata sends must share one FIFO: {order}"
    release.set()
    await first
    await second
    assert order == [("start", "FIRST"), ("done", "FIRST"), ("start", "SECOND"), ("done", "SECOND")], order


@pytest.mark.asyncio
async def test_same_topic_concurrent_sends_still_serialize_in_order():
    """Upstream's #114396 guarantee holds within one topic: A A A B B B, never interleaved."""
    adapter = _adapter()
    order: list = []

    async def fake_send_message(text: str, **_kw):
        await asyncio.sleep(0)  # yield like a real round-trip so an unlocked loop interleaves
        order.append(text.split()[0])
        return MagicMock(message_id=len(order))

    adapter._bot.send_message = fake_send_message
    long = lambda tag: "\n".join(" ".join([tag] * 30) for _ in range(90))  # noqa: E731
    metadata = {"thread_id": "42"}

    await asyncio.gather(
        adapter.send("1", long("REPORT"), metadata=metadata),
        adapter.send("1", long("ALERT"), metadata=metadata),
    )

    assert len(order) >= 4
    switches = sum(1 for a, b in zip(order, order[1:]) if a != b)
    assert switches == 1, order
