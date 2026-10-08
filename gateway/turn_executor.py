"""Unbounded executor for gateway turn bodies (see ``GatewayRunner._get_executor`` in gateway/run.py)."""

from __future__ import annotations

import concurrent.futures
import threading
from collections import deque


class _UnboundedThreadExecutor(concurrent.futures.Executor):
    """One thread per submitted work item; no queue cap on accepted turns.

    ``ThreadPoolExecutor(max_workers=None)`` is NOT unbounded (it is ``min(32, cpu_count + 4)``),
    which is the same silent queue at a larger number. Exposes ``_threads`` and ``_shutdown`` like
    ``ThreadPoolExecutor`` so ``_stop_pool`` / ``_shutdown_executor`` join and count its workers.
    Not ``tools.daemon_pool.DaemonThreadPoolExecutor(sys.maxsize)``: that keeps idle workers alive
    until shutdown, whereas here each thread exits when its turn ends.

    ``submit`` only enqueues. A dispatcher thread calls ``Thread.start``, so a caller on the event
    loop does not hold ``_lock`` across OS thread startup. The worker is registered in ``_threads``
    before ``start`` returns, including when ``start`` itself runs the worker to completion.
    ``shutdown(wait=True)`` drains accepted work. ``shutdown(wait=False)`` does not wait on a
    ``Thread.start`` that has not returned.
    """

    def __init__(self, thread_name_prefix: str = ""):
        self._prefix = thread_name_prefix
        self._threads: set = set()
        self._shutdown = False
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._n = 0
        self._queue: deque = deque()
        self._inflight = 0
        self._dispatcher: threading.Thread | None = None

    def submit(self, fn, /, *args, **kwargs):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        dispatcher: threading.Thread | None = None
        with self._cv:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._dispatcher is None:
                dispatcher = threading.Thread(
                    target=self._dispatch,
                    name=f"{self._prefix}-dispatch" if self._prefix else "hermes-executor-dispatch",
                    daemon=True,
                )
                self._dispatcher = dispatcher
            self._n += 1
            self._queue.append((fut, fn, args, kwargs, self._n))
            self._cv.notify()
        # Start the dispatcher outside the lock so the first submit does not couple admission to
        # OS thread creation either.
        if dispatcher is not None:
            dispatcher.start()
        return fut

    def _dispatch(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._shutdown:
                    self._cv.wait()
                if not self._queue:
                    self._cv.notify_all()
                    return
                fut, fn, args, kwargs, n = self._queue.popleft()
                self._inflight += 1
                thread = self._make_worker(fut, fn, args, kwargs, n)
            self._start_registered(thread, fut)

    def _make_worker(self, fut, fn, args, kwargs, n: int) -> threading.Thread:
        def _run():
            # Register on the worker, which is running, so ``_threads`` only holds joinable
            # threads. Adding after ``Thread.start`` returns loses the race where start joins
            # the worker first; adding before start hands ``_stop_pool`` a thread join() rejects.
            current = threading.current_thread()
            with self._cv:
                self._threads.add(current)
                self._cv.notify_all()
            try:
                if not fut.set_running_or_notify_cancel():
                    return
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - mirror ThreadPoolExecutor
                    fut.set_exception(exc)
            finally:
                with self._cv:
                    self._threads.discard(current)
                    self._inflight -= 1
                    self._cv.notify_all()

        return threading.Thread(target=_run, name=f"{self._prefix}_{n}", daemon=True)

    def _start_registered(self, thread: threading.Thread, fut) -> None:
        try:
            # Outside the executor lock: a slow start must not stall another submit, including one
            # issued on the event loop. The worker adds itself to ``_threads`` once it is running.
            thread.start()
        except BaseException as exc:  # noqa: BLE001 - thread-limit and shutdown races are results
            with self._cv:
                self._threads.discard(thread)
                self._inflight -= 1
                self._cv.notify_all()
            try:
                fut.set_exception(exc)
            except concurrent.futures.InvalidStateError:
                pass

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        with self._cv:
            self._shutdown = True
            if cancel_futures:
                while self._queue:
                    fut, _fn, _args, _kwargs, _n = self._queue.popleft()
                    fut.cancel()
            self._cv.notify_all()
            if not wait:
                # Do not wait for a Thread.start that has not returned. A start that never
                # enters the worker is absent from ``_threads``; joining it would raise.
                return
            while self._queue or self._inflight:
                self._cv.wait()
            threads = [thread for thread in self._threads if thread.is_alive()]
        for thread in threads:
            thread.join()
