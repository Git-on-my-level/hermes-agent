"""Blocked best-effort gateway writers must not keep a process alive."""
import os
import queue
import subprocess
import sys
import threading

import pytest


def assert_child_exits(script, tmp_path, *args):
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    child = subprocess.Popen([sys.executable, '-c', script, *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert ready.get(timeout=30).strip() == 'ready'
        child.wait(timeout=5)
        assert child.returncode == 0, child.stderr.read()
    finally:
        if child.poll() is None:
            child.kill()  # Only this test-owned subprocess.
            child.wait(timeout=5)
        reader.join(timeout=5)
        child.stdout.close()
        child.stderr.close()


def test_blocked_status_preparation_does_not_join_at_interpreter_exit(tmp_path):
    assert_child_exits('''
import asyncio
import threading
from gateway import status
from gateway.status_async import publish_runtime_status, flush_pending
held = threading.Lock()
held.acquire()
entered = threading.Event()
def blocked_publish(**fields):
    entered.set()
    with held:
        pass
status.publish_runtime_status = blocked_publish
async def main():
    publish_runtime_status(platform='telegram', platform_state='retrying')
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        await asyncio.wait_for(flush_pending(), 0.05)
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError('status worker was not blocked')
asyncio.run(main())
print('ready', flush=True)
# Normal interpreter exit; the held status lock is deliberately never released.
''', tmp_path)


@pytest.mark.parametrize('mode', ['explicit', 'atexit', 'stderr-explicit', 'stderr-atexit'])
def test_blocked_console_pipe_has_bounded_close_and_interpreter_exit(tmp_path, mode):
    assert_child_exits('''
import logging
import os
import sys
import threading
from gateway.queued_console import QueuedConsoleHandler
read_fd, write_fd = os.pipe()
entered = threading.Event()
class UnreadPipe:
    def write(self, text):
        entered.set()
        os.write(write_fd, text.encode('utf-8'))
    def flush(self):
        pass
if sys.argv[1].startswith('stderr-'):
    # A real standard TextIO pipe is flushed by CPython even after atexit.
    # Identify entry into descriptor write without changing the stream object.
    original_write = os.write
    def write(fd, payload):
        if fd == sys.stderr.fileno():
            entered.set()
        return original_write(fd, payload)
    os.write = write
    handler = QueuedConsoleHandler(sys.stderr)
else:
    handler = QueuedConsoleHandler(UnreadPipe())
handler.handle(logging.LogRecord('exit-test', logging.ERROR, '<test>', 0, 'x' * 1048576, (), None))
assert entered.wait(10)
print('ready', flush=True)
if sys.argv[1].endswith('explicit'):
    handler.close()
    handler.close()  # Idempotent even with a stuck writer.
    logging.shutdown()  # Must not acquire a blocked sink's Handler lock.
# Normal exit also runs atexit and logging.shutdown without reading the pipe.
''', tmp_path, mode)


def test_console_preserves_record_filters_and_profile_a_b_a_context():
    import copy
    from contextvars import ContextVar
    import io
    import logging
    from gateway.queued_console import QueuedConsoleHandler

    complete = threading.Event()
    profile = ContextVar('console_test_profile', default='unbound')

    class Stream(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if self.getvalue().count('\n') == 3:
                complete.set()
            return result

    class ReplaceRecord(logging.Filter):
        def filter(self, record):
            replacement = copy.copy(record) if sys.version_info >= (3, 12) else record
            replacement.profile = profile.get()
            replacement.msg = 'filtered-' + record.msg
            return replacement if sys.version_info >= (3, 12) else True

    stream = Stream()
    handler = QueuedConsoleHandler(stream)
    handler.setFormatter(logging.Formatter('%(profile)s:%(message)s'))
    handler.addFilter(ReplaceRecord())
    try:
        for index, name in enumerate(('A', 'B', 'A')):
            token = profile.set(name)
            try:
                handler.handle(logging.LogRecord('console-test', logging.INFO, __file__, 0, str(index), (), None))
            finally:
                profile.reset(token)
        assert complete.wait(5)
        assert stream.getvalue() == 'A:filtered-0\nB:filtered-1\nA:filtered-2\n'
    finally:
        handler.close()
