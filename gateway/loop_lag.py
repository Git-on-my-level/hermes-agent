"""Diagnose stalls while the loop is still blocked, without changing recovery policy."""
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)


def start_loop_lag_watchdog(loop, *, threshold=1.0, interval=0.25, rate_limit=30.0):
    """Call on the monitored loop thread. The probe thread owns no agent locks."""
    owner = threading.get_ident()
    stopped = threading.Event()
    acknowledged = time.monotonic()
    pending = False

    def tick():
        nonlocal acknowledged, pending
        acknowledged = time.monotonic()
        pending = False

    def watch():
        nonlocal pending
        last_warning = float('-inf')
        while not stopped.wait(interval):
            now = time.monotonic()
            if now - acknowledged > threshold and now - last_warning >= rate_limit:
                frame = sys._current_frames().get(owner)
                stack = ''.join(traceback.format_stack(frame)) if frame else '<thread exited>'
                logger.warning('Gateway loop lag %.3fs (thread %d):\n%s', now - acknowledged, owner, stack)
                last_warning = now
            if not pending:
                pending = True
                try:
                    loop.call_soon_threadsafe(tick)
                except RuntimeError:
                    return

    thread = threading.Thread(target=watch, name='gateway-loop-lag', daemon=True)
    thread.start()
    return stopped
