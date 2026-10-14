"""Console writes must not hold a logging RLock on an asyncio caller."""
import atexit
import copy
from contextvars import copy_context
import logging
import queue
from logging.handlers import QueueListener


class QueuedConsoleHandler(logging.StreamHandler):
    def __init__(self, stream):
        super().__init__(stream)
        self._queue = queue.SimpleQueue()
        # Keep formatting/redaction on the writer thread and preserve the public
        # handler's formatter/level configuration.
        owner = self
        class Sink(logging.StreamHandler):
            def handle(self, item):
                context, record = item
                return context.run(super().handle, record)

            def format(self, record):
                return owner.format(record)

            def filter(self, record):
                return owner.filter(record)
        # The sink has its own lock: logging.shutdown can close the producer
        # while holding its lock without deadlocking the writer drain.
        self._listener = QueueListener(self._queue, Sink(stream))
        self._listener.start()
        atexit.register(self._listener.stop)

    def handle(self, record):
        self._queue.put((copy_context(), copy.copy(record)))
        return True

    def close(self):
        listener = getattr(self, '_listener', None)
        if listener is not None:
            self._listener = None
            atexit.unregister(listener.stop)
            listener.stop()
        super().close()
