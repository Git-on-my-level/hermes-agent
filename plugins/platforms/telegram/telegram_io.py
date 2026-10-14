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
        task_holder = []

        async def tracked():
            task_holder.append(asyncio.current_task())
            return await awaitable

        future = asyncio.run_coroutine_threadsafe(tracked(), self.loop)
        try:
            return await asyncio.wrap_future(future)
        finally:
            # wrap_future cancel cannot stop a running concurrent future; cancel the
            # I/O task on its own loop so a timed-out send cannot still deliver.
            if not future.done():
                def cancel_io():
                    for task in task_holder:
                        if task is not None and not task.done():
                            task.cancel()
                self.loop.call_soon_threadsafe(cancel_io)
                future.cancel()


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


def _is_polling_conflict(error):
    text = str(error).lower()
    return (
        error.__class__.__name__.lower() == 'conflict'
        or 'terminated by other getupdates request' in text
        or 'another bot instance is running' in text
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
    if original_queue.maxsize == 0:
        # Pause polling/webhook intake at 256 updates waiting for dispatch.
        original_queue = asyncio.Queue(maxsize=256)
        app.update_queue = original_queue

    class QueueBridge:
        async def put(self, item):
            # Do not return to PTB (which would ack getUpdates) until the gateway
            # queue holds the item. A bounded queue pauses polling at capacity.
            done = asyncio.get_running_loop().create_future()

            def finish(task):
                cancelled = task.cancelled()
                exc = None if cancelled else task.exception()

                def settle():
                    if done.done():
                        return
                    if cancelled:
                        done.cancel()
                    elif exc is not None:
                        done.set_exception(exc)
                    else:
                        done.set_result(None)
                io.loop.call_soon_threadsafe(settle)

            def deliver():
                task = gateway_loop.create_task(original_queue.put(item))
                task.add_done_callback(finish)

            gateway_loop.call_soon_threadsafe(deliver)
            await done

    class IsolatedUpdater(Updater):
        async def start_polling(self, *args, **kwargs):
            callback = kwargs.get('error_callback')
            if callback:
                def wrapped(error):
                    # PTB retries unless the stop event is set before this returns.
                    if _is_polling_conflict(error):
                        self.disarm_polling()
                    gateway_loop.call_soon_threadsafe(callback, error)
                kwargs['error_callback'] = wrapped
            self.update_queue = QueueBridge()
            try:
                await io.run(super().start_polling(*args, **kwargs))
                return original_queue
            except BaseException:
                self.update_queue = original_queue
                raise

        def disarm_polling(self):
            event = None
            for attr in ('_Updater__polling_task_stop_event', '_polling_task_stop_event'):
                candidate = getattr(self, attr, None)
                if isinstance(candidate, asyncio.Event):
                    event = candidate
                    break
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
            try:
                if self._httpd is not None:
                    return await super().stop()
                return await io.run(super().stop())
            finally:
                self.update_queue = original_queue

    app.updater = IsolatedUpdater(app.bot, original_queue)
