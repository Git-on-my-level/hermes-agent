"""One transport loop, separate from gateway dispatch and its agent executor.

PTB HTTP requests and Updater polling live here. Application handlers, status
callbacks and asyncio queues remain on the gateway loop. This isolates blocking
agent locks, but cannot isolate the process GIL or OS scheduler starvation.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from tools.daemon_pool import DaemonThreadPoolExecutor
import threading
import json
import weakref


class TelegramIOLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        # A slow fsync must not occupy the DNS/TLS executor. Pending writes
        # have not been accepted/acknowledged, so they need not hold exit open.
        self.inbox_executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix='telegram-inbox')

        def serve():
            asyncio.set_event_loop(self.loop)
            self.loop.set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix='telegram-build'))
            self.loop.run_forever()

        self.thread = threading.Thread(target=serve, name='telegram-io', daemon=True)
        self.thread.start()

    async def build(self, factory, *args, **kwargs):
        return await self.run(asyncio.to_thread(factory, *args, **kwargs))

    async def run_inbox(self, factory, *args):
        async def work():
            return await self.loop.run_in_executor(self.inbox_executor, partial(factory, *args))
        return await self.run(work())

    async def run(self, awaitable):
        if asyncio.get_running_loop() is self.loop:
            return await awaitable
        future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # wrap_future chains cancellation; keep the ownership explicit.
            future.cancel()
            raise


# Accessed only on the owning gateway loop. A late committed write from a
# cancelled old polling action must wake a replacement pump for the same inbox.
_inbox_readers = {}


def _wake_inbox_readers(key):
    for reader in tuple(_inbox_readers.get(key, ())):
        reader.wake.set()


# Created only by gateway-loop setup, never from an agent turn.
_io = None


def io_loop():
    global _io
    if _io is None:
        _io = TelegramIOLoop()
    return _io


def isolate_request(request):
    """PTB reads complete response bytes inside do_request; no stream crosses loops."""
    if not callable(getattr(type(request), '_build_client', None)):
        return request  # optional SDK doubles
    io = io_loop()

    class IsolatedRequest(type(request)):
        __slots__ = ()

        async def do_request(self, *args, **kwargs):
            return await io.run(super().do_request(*args, **kwargs))

        async def initialize(self):
            return await io.run(super().initialize())

        async def shutdown(self):
            return await io.run(super().shutdown())

        async def rebuild_client(self):
            async def replace():
                old = self._client
                self._client = await asyncio.to_thread(self._build_client)
                return old
            return await io.run(replace())

        async def close_client(self, client):
            return await io.run(client.aclose())

    request.__class__ = IsolatedRequest
    return request


def _is_polling_conflict(error):
    return (
        error.__class__.__name__.lower() == 'conflict'
        or 'terminated by other getupdates request' in str(error).lower()
        or 'another bot instance is running' in str(error).lower()
    )


def isolate_updater(app):
    """Move only polling to the I/O loop; webhook serving stays on gateway."""
    from telegram.ext import Updater
    if not isinstance(Updater, type) or not isinstance(app.updater, Updater):
        return
    if callable(getattr(type(app.updater), 'disarm_polling', None)):
        return
    gateway_loop = asyncio.get_running_loop()
    io = io_loop()
    original_queue = app.update_queue
    if original_queue.maxsize == 0 and original_queue.empty():
        original_queue = asyncio.Queue(maxsize=256)
        app.update_queue = original_queue

    from .update_inbox import UpdateInbox
    from hermes_constants import get_hermes_home
    adapter = getattr(app, 'adapter', None)
    directory = getattr(adapter, '_update_receipt_dir', None) or get_hermes_home()
    inbox = UpdateInbox(directory, app.bot.token.split(':', 1)[0])
    inbox_key = (gateway_loop, inbox.path)

    class QueueBridge:
        def __init__(self):
            self.task = None
            self.closing = False
            self.accepting = False
            self.cursor = 0
            self.wake = asyncio.Event()

        async def put(self, item):
            # Durability, not bounded RAM, is the polling acceptance boundary.
            # A disk failure propagates: PTB must not advance its offset.
            self.accepting = True
            try:
                def persist():
                    inbox.put(item)
                    # Runs even if cancellation abandoned this write's waiter.
                    # All registry lookup/mutation remains on the gateway loop.
                    try:
                        gateway_loop.call_soon_threadsafe(_wake_inbox_readers, inbox_key)
                    except RuntimeError:
                        pass  # Process loop ended; committed rows replay on next boot.
                await io.run_inbox(persist)
            finally:
                self.accepting = False

        def backpressured(self):
            return self.accepting or original_queue.full()

        async def pump(self):
            from telegram import Update
            while not self.closing:
                self.wake.clear()
                rows = await io.run_inbox(inbox.after, self.cursor)
                if not rows:
                    await self.wake.wait()
                    continue
                for sequence, payload, kind in rows:
                    item = Update.de_json(json.loads(payload), app.bot) if kind == 'update' else json.loads(payload)
                    await original_queue.put(item)
                    self.cursor = sequence
                    if self.closing:
                        return

        def start(self):
            self.closing = False
            self.cursor = 0  # Replay uncompleted rows on every polling generation.
            self.wake.set()
            _inbox_readers.setdefault(inbox_key, weakref.WeakSet()).add(self)
            self.task = gateway_loop.create_task(self.pump())

        async def finish(self):
            self.closing = True
            readers = _inbox_readers.get(inbox_key)
            if readers is not None:
                readers.discard(self)
                if not readers:
                    _inbox_readers.pop(inbox_key, None)
            if self.task is not None:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                self.task = None
            # Gateway-queued and pump-held items remain durable until admission
            # completion. No dispatcher drain is needed to stop polling.

    bridge = QueueBridge()

    class IsolatedUpdater(Updater):
        def polling_backpressured(self):
            return bridge.backpressured()

        async def complete_updates(self, update_ids):
            await io.run_inbox(inbox.complete, update_ids)

        async def start_polling(self, *args, **kwargs):
            if self.running or bridge.task is not None:
                raise RuntimeError("Telegram polling is already starting or running")
            callback = kwargs.get('error_callback')
            if callback:
                def on_error(error):
                    # PTB checks this synchronously after its callback returns.
                    # Stop a conflicting retry before deferring gateway recovery.
                    if _is_polling_conflict(error):
                        self.disarm_polling()
                    gateway_loop.call_soon_threadsafe(callback, error)
                kwargs['error_callback'] = on_error
            self.update_queue = bridge
            bridge.start()
            try:
                await io.run(super().start_polling(*args, **kwargs))
            except BaseException:
                if self.running:
                    await io.run(super().stop())
                await bridge.finish()
                raise
            return original_queue

        async def start_webhook(self, *args, **kwargs):
            if self.running or bridge.task is not None:
                raise RuntimeError("This Updater is already running")
            self.update_queue = original_queue
            return await super().start_webhook(*args, **kwargs)

        def disarm_polling(self):
            event = next((value for name in (
                '_Updater__polling_task_stop_event', '_polling_task_stop_event'
            ) if isinstance(value := getattr(self, name, None), asyncio.Event)), None)
            if event is None or event.is_set():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is io.loop:
                event.set()
            else:
                io.loop.call_soon_threadsafe(event.set)

        async def stop(self):
            if self._httpd is not None:
                return await super().stop()
            async def stop_polling():
                # PTB normally waits for its action (including queue.put) before
                # observing the stop event. Cancel it instead: accepted items are
                # already committed, and unaccepted batch tails remain on Telegram.
                task = getattr(self, '_Updater__polling_task', None)
                if task is not None:
                    task.cancel()
                return await super(IsolatedUpdater, self).stop()
            try:
                return await io.run(stop_polling())
            finally:
                await bridge.finish()

    app.updater = IsolatedUpdater(app.bot, original_queue)
    app.updater.durable_inbox = inbox
