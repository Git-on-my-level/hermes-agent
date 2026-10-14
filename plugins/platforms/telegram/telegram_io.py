"""One transport loop, separate from gateway dispatch and its agent executor.

PTB HTTP requests and Updater polling live here. Application handlers, status
callbacks and asyncio queues remain on the gateway loop. This isolates blocking
agent locks, but cannot isolate the process GIL or OS scheduler starvation.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import queue


class TelegramIOLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()

        def serve():
            asyncio.set_event_loop(self.loop)
            self.loop.set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix='telegram-build'))
            self.loop.run_forever()

        self.thread = threading.Thread(target=serve, name='telegram-io', daemon=True)
        self.thread.start()

    async def build(self, factory, *args, **kwargs):
        return await self.run(asyncio.to_thread(factory, *args, **kwargs))

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

    class QueueBridge:
        def __init__(self):
            # Acceptance is synchronous into a real bounded queue, before PTB
            # advances its offset. No update exists only as a scheduled callback.
            self.pending = queue.Queue(maxsize=256)
            self.task = None
            self.closing = False

        async def put(self, item):
            while True:
                try:
                    self.pending.put_nowait(item)
                    return
                except queue.Full:
                    await asyncio.sleep(0.01)

        async def pump(self):
            while not self.closing or not self.pending.empty():
                # Keep gateway admission bounded as well. Application retains
                # its existing update-processor concurrency policy.
                if not self.closing and original_queue.qsize() >= 256:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    item = self.pending.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                await original_queue.put(item)
                self.pending.task_done()

        def start(self):
            self.closing = False
            self.task = gateway_loop.create_task(self.pump())

        async def finish(self):
            self.closing = True
            if self.task is not None:
                await self.task
                self.task = None

    bridge = QueueBridge()

    class IsolatedUpdater(Updater):
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
            try:
                return await io.run(super().stop())
            finally:
                await bridge.finish()

    app.updater = IsolatedUpdater(app.bot, original_queue)
