"""Console writes and shutdown must never wait indefinitely for a blocked sink."""
import atexit
import copy
from contextvars import copy_context
import logging
import os
import queue
import sys
import threading
from logging.handlers import QueueListener


class _BoundedQueueListener(QueueListener):
    def stop(self):
        thread = self._thread
        if thread is None:
            return
        self.enqueue_sentinel()
        if thread is not threading.current_thread():
            thread.join(timeout=0.5)
        # QueueListener's worker is a daemon. A permanently blocked stream must
        # not turn a best-effort console drain into an interpreter-exit barrier.
        self._thread = None


class QueuedConsoleHandler(logging.StreamHandler):
    def __init__(self, stream):
        super().__init__(stream)
        self._queue = queue.SimpleQueue()
        owner = self
        raw_fd = None
        if any(stream is standard for standard in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)):
            try:
                # Do not hold a standard TextIO lock while an unread pipe is
                # blocked: CPython flushes these streams again at finalization.
                # Native Windows consoles need Python's console encoding path.
                if os.name != 'nt' or not stream.isatty():
                    raw_fd = stream.fileno()
            except (AttributeError, OSError, ValueError):
                pass

        class Sink:
            # Not a logging.Handler: registering a second Handler would make
            # logging.shutdown acquire its lock while emit is stuck in write.
            # Only the single listener thread writes/flushes this stream.
            def handle(self, item):
                context, record = item
                context.run(self.emit, record)

            def emit(self, record):
                try:
                    filtered = owner.filter(record)
                    if not filtered:
                        return
                    if isinstance(filtered, logging.LogRecord):
                        record = filtered
                    text = owner.format(record) + owner.terminator
                    if raw_fd is None:
                        stream.write(text)
                        stream.flush()
                    else:
                        data = memoryview(text.encode(getattr(stream, 'encoding', None) or 'utf-8',
                                                      getattr(stream, 'errors', None) or 'backslashreplace'))
                        while data:
                            written = os.write(raw_fd, data)
                            if not written:
                                raise OSError('Console write made no progress')
                            data = data[written:]
                except RecursionError:
                    raise
                except Exception:
                    owner.handleError(record)

        self._listener = _BoundedQueueListener(self._queue, Sink())
        self._listener.start()
        atexit.register(self.close)

    def handle(self, record):
        if not self._closed:
            self._queue.put((copy_context(), copy.copy(record)))
        return True

    def flush(self):
        # StreamHandler.flush on a shutdown caller can wait for the stream's
        # writer lock. Every emit already flushes on the daemon writer instead.
        pass

    def close(self):
        listener = getattr(self, '_listener', None)
        if listener is not None:
            self._listener = None
            atexit.unregister(self.close)
            listener.stop()
        super().close()
