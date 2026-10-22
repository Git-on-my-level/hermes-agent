"""Handoff contracts between the Telegram I/O loop and the gateway loop."""
import asyncio
import threading

import pytest

from plugins.platforms.telegram.telegram_io import io_loop, isolate_updater


@pytest.mark.asyncio
async def test_cancelled_run_cancels_the_io_task():
    started = threading.Event()
    cancelled = threading.Event()

    async def hung():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    waiter = asyncio.create_task(io_loop().run(hung()))
    assert await asyncio.to_thread(started.wait, 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await asyncio.to_thread(cancelled.wait, 2)


@pytest.mark.asyncio
async def test_conflict_callback_sets_stop_event_before_gateway_runs(monkeypatch):
    pytest.importorskip("telegram")
    from telegram.ext import Application, Updater

    captured = {}

    async def fake_start_polling(self, *args, **kwargs):
        captured["callback"] = kwargs.get("error_callback")
        captured["queue"] = self.update_queue
        return self.update_queue

    monkeypatch.setattr(Updater, "start_polling", fake_start_polling)

    app = Application.builder().token("123:fake").build()
    isolate_updater(app)
    gateway_ran = []

    def gateway_cb(error):
        assert app.updater._Updater__polling_task_stop_event.is_set()
        gateway_ran.append(error)

    await app.updater.start_polling(error_callback=gateway_cb)
    stop = asyncio.Event()
    app.updater._Updater__polling_task_stop_event = stop

    class Conflict(Exception):
        pass

    async def fire():
        captured["callback"](Conflict("Conflict: terminated by other getUpdates request"))
        assert stop.is_set()

    await io_loop().run(fire())
    await asyncio.sleep(0)
    assert gateway_ran

    delattr(app.updater, "_Updater__polling_task_stop_event")
    legacy = asyncio.Event()
    app.updater._polling_task_stop_event = legacy

    async def disarm_legacy():
        app.updater.disarm_polling()
        assert legacy.is_set()
        delattr(app.updater, "_polling_task_stop_event")
        app.updater.disarm_polling()

    await io_loop().run(disarm_legacy())
    app.updater._Updater__polling_task_stop_event = asyncio.Event()
    app.updater._running = True
    await app.updater.stop()
    for request in app.bot._request:
        await request.shutdown()


@pytest.mark.asyncio
async def test_polling_handoff_accepts_with_bounded_gateway_backpressure(monkeypatch):
    pytest.importorskip("telegram")
    from telegram.ext import Application, Updater

    captured = {}

    async def fake_start_polling(self, *args, **kwargs):
        captured["queue"] = self.update_queue
        return self.update_queue

    monkeypatch.setattr(Updater, "start_polling", fake_start_polling)

    app = Application.builder().token("123:fake").build()
    isolate_updater(app)
    await app.updater.start_polling()
    bridge = captured["queue"]
    assert bridge is not app.update_queue
    assert app.update_queue.maxsize == 256

    pending = asyncio.run_coroutine_threadsafe(bridge.put("first"), io_loop().loop)
    await asyncio.wait_for(asyncio.wrap_future(pending), 1)
    assert await asyncio.wait_for(app.update_queue.get(), 1) == "first"

    for i in range(256):
        app.update_queue.put_nowait(i)
    blocked = asyncio.run_coroutine_threadsafe(bridge.put("blocked"), io_loop().loop)
    await asyncio.wait_for(asyncio.wrap_future(blocked), 1)
    assert app.update_queue.qsize() == 256
    assert app.update_queue.get_nowait() == 0
    for _ in range(100):
        if app.update_queue.qsize() == 256:
            break
        await asyncio.sleep(0.01)
    assert app.update_queue.qsize() == 256
    assert [app.update_queue.get_nowait() for _ in range(256)] == list(range(1, 256)) + ["blocked"]
    app.updater._running = True
    await app.updater.stop()
    for request in app.bot._request:
        await request.shutdown()


@pytest.mark.asyncio
async def test_webhook_update_queue_stays_on_gateway_loop():
    pytest.importorskip("telegram")
    from telegram.ext import Application

    app = Application.builder().token("123:fake").build()
    isolate_updater(app)
    assert app.updater.update_queue is app.update_queue
    await app.updater.update_queue.put("hook")
    assert app.update_queue.get_nowait() == "hook"
    for request in app.bot._request:
        await request.shutdown()
