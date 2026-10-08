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

    ``submit`` only enqueues. A dispatcher thread calls ``Thread.start`` outside this lock, so a
    caller on the event loop does not stall on OS thread startup. A dequeued future is a pending
    start until its worker is inside ``_run``: it is not in ``_threads`` until then, and ``join()``
    is never asked to join that unstarted thread. ``cancel_futures`` cancels queued
    futures and pending starts together, so a body that has not begun does not run after shutdown.
    On entry the worker adds itself to ``_threads`` and drops that entry before releasing the lock
    if the future was cancelled. After the lock is released, ``_threads`` holds only workers whose
    bodies will run.
    ``shutdown(wait=True)`` drains accepted work that is still going to run. ``shutdown(wait=False)``
    does not wait for a ``Thread.start`` that has not returned.
    """

    def __init__(self, thread_name_prefix: str = ""):
        self._prefix = thread_name_prefix
        self._threads: set = set()
        self._shutdown = False
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._n = 0
        self._queue: deque = deque()
        self._pending: set = set()
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
                # Same critical section as the pop: shutdown must see this future as a pending
                # start, not as neither queued nor running.
                self._pending.add(fut)
            self._start_registered(thread, fut)

    def _make_worker(self, fut, fn, args, kwargs, n: int) -> threading.Thread:
        def _run():
            # This thread is running, so it is joinable. Shutdown captured ``_pending`` under
            # this lock and cancels those futures before it returns; ``set_running_or_notify_cancel``
            # is what makes the two sides exclusive. A cancel that wins leaves the body unrun and
            # removes this thread before the lock is released. Adding the thread object before
            # ``start`` would make ``join()`` raise; adding it after ``start`` returns loses the
            # race where start joins the worker first.
            current = threading.current_thread()
            with self._cv:
                self._pending.discard(fut)
                # In ``_threads`` before the future can become running, so a shutdown that loses
                # the cancel race still snapshots a joinable worker. A cancel that wins takes the
                # thread back out before this lock is released.
                self._threads.add(current)
                run_body = fut.set_running_or_notify_cancel()
                if not run_body:
                    self._threads.discard(current)
                self._cv.notify_all()
            try:
                if not run_body:
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
                self._pending.discard(fut)
                self._threads.discard(thread)
                self._inflight -= 1
                self._cv.notify_all()
            if not fut.cancelled():
                try:
                    fut.set_exception(exc)
                except concurrent.futures.InvalidStateError:
                    pass

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        with self._cv:
            self._shutdown = True
            queued: list = []
            pending: list = []
            if cancel_futures:
                while self._queue:
                    fut, _fn, _args, _kwargs, _n = self._queue.popleft()
                    queued.append(fut)
                pending = list(self._pending)
            self._cv.notify_all()
        # ``Future.cancel`` runs done callbacks. Keep that outside ``_cv`` so a callback can take
        # the executor lock. Cancel still happens before ``wait=False`` returns and before the
        # ``wait=True`` drain, and it is the same set captured under the lock.
        if cancel_futures:
            for fut in queued:
                fut.cancel()
            for fut in pending:
                fut.cancel()
        if not wait:
            # Do not wait for a Thread.start that has not entered the worker. That thread is not
            # joinable and is not in ``_threads``. With cancel_futures, its body is cancelled.
            return
        with self._cv:
            while self._queue or self._inflight:
                self._cv.wait()
            threads = [thread for thread in self._threads if thread.is_alive()]
        for thread in threads:
            thread.join()
