"""One transport loop, separate from gateway dispatch and its agent executor.

PTB HTTP requests and Updater polling live here. Application handlers, status
callbacks and asyncio queues remain on the gateway loop. This isolates blocking
agent locks, but cannot isolate the process GIL or OS scheduler starvation.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading


class TelegramIOLoop:
    def __init__(self):
        ready = threading.Event()

        def serve():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix='telegram-build'))
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=serve, name='telegram-io', daemon=True)
        self.thread.start()
        ready.wait()

    async def build(self, factory, *args, **kwargs):
        return await self.run(asyncio.to_thread(factory, *args, **kwargs))

    async def run(self, awaitable):
        if asyncio.get_running_loop() is self.loop:
            return await awaitable
        future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
        return await asyncio.wrap_future(future)


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

    request.__class__ = IsolatedRequest
    return request


def isolate_updater(app):
    """Move only polling to the I/O loop; webhook serving stays on gateway."""
    from telegram.ext import Updater
    if not isinstance(Updater, type) or not isinstance(app.updater, Updater):
        return
    gateway_loop = asyncio.get_running_loop()
    io = io_loop()
    original_queue = app.update_queue

    class QueueBridge:
        def __init__(self):
            self.slots = asyncio.Semaphore(256)

        async def put(self, item):
            # Bounded handoff, with no agent callback on the transport loop.
            # Keep polling through a short dispatch stall without unbounded buffering.
            await self.slots.acquire()

            def admitted(done):
                io.loop.call_soon_threadsafe(self.slots.release)

            def deliver():
                task = gateway_loop.create_task(original_queue.put(item))
                task.add_done_callback(admitted)

            gateway_loop.call_soon_threadsafe(deliver)

    class IsolatedUpdater(Updater):
        async def start_polling(self, *args, **kwargs):
            callback = kwargs.get('error_callback')
            if callback:
                kwargs['error_callback'] = lambda error: gateway_loop.call_soon_threadsafe(callback, error)
            await io.run(super().start_polling(*args, **kwargs))
            return original_queue

        def disarm_polling(self):
            io.loop.call_soon_threadsafe(self._Updater__polling_task_stop_event.set)

        async def stop(self):
            if self._httpd is not None:
                return await super().stop()
            return await io.run(super().stop())

    app.updater = IsolatedUpdater(app.bot, QueueBridge())
