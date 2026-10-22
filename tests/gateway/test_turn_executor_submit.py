"""The turn executor must not stall the event loop inside Thread.start."""

from __future__ import annotations

import threading
import time

from gateway.turn_executor import _UnboundedThreadExecutor


def test_submit_returns_while_another_worker_is_still_starting(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_start = threading.Thread.start

    def slow_start(self, *args, **kwargs):
        if self.name.startswith("hermes-gateway_"):
            started.set()
            assert release.wait(2), "worker start was never released"
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    executor = _UnboundedThreadExecutor(thread_name_prefix="hermes-gateway")
    try:
        first = executor.submit(lambda: "first")
        assert started.wait(1), "dispatcher never reached Thread.start"
        began = time.monotonic()
        second = executor.submit(lambda: "second")
        assert time.monotonic() - began < 0.2, "submit blocked on another worker's thread start"
        release.set()
        assert first.result(timeout=2) == "first"
        assert second.result(timeout=2) == "second"
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_shutdown_observes_a_worker_that_is_still_starting():
    release = threading.Event()
    executor = _UnboundedThreadExecutor(thread_name_prefix="hermes-gateway")
    try:
        future = executor.submit(lambda: release.wait(2))
        for _ in range(50):
            if executor._threads:
                break
            time.sleep(0.01)
        executor.shutdown(wait=False, cancel_futures=True)
        assert any(thread.name.startswith("hermes-gateway_") for thread in executor._threads)
        release.set()
        assert future.result(timeout=2) is True
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_submit_after_shutdown_is_refused():
    executor = _UnboundedThreadExecutor(thread_name_prefix="hermes-gateway")
    executor.shutdown(wait=True)
    try:
        executor.submit(lambda: None)
    except RuntimeError as exc:
        assert "shutdown" in str(exc)
    else:
        raise AssertionError("submit after shutdown was accepted")


def test_start_that_joins_the_worker_does_not_retain_it(monkeypatch):
    real_start = threading.Thread.start

    def joining_start(self, *args, **kwargs):
        if self.name.startswith("review-worker_"):
            real_start(self, *args, **kwargs)
            self.join()
            return None
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", joining_start)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    future = executor.submit(lambda: 7)
    assert future.result(timeout=2) == 7
    executor.shutdown(wait=True)
    assert not [thread for thread in executor._threads if thread.name.startswith("review-worker_")]


def test_shutdown_wait_drains_work_the_dispatcher_has_not_started(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real_dispatch = _UnboundedThreadExecutor._dispatch

    def gated(self):
        entered.set()
        assert gate.wait(2), "dispatcher gate was never released"
        return real_dispatch(self)

    monkeypatch.setattr(_UnboundedThreadExecutor, "_dispatch", gated)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    future = executor.submit(lambda: "done")
    assert entered.wait(1), "dispatcher never blocked"
    assert not future.done()
    holder = threading.Thread(target=lambda: executor.shutdown(wait=True))
    holder.start()
    time.sleep(0.05)
    assert holder.is_alive(), "shutdown(wait=True) returned while the accepted future was pending"
    assert not future.done()
    gate.set()
    holder.join(2)
    assert not holder.is_alive()
    assert future.result(timeout=1) == "done"


def test_shutdown_wait_false_returns_while_thread_start_hangs(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_start = threading.Thread.start

    def hanging_start(self, *args, **kwargs):
        if self.name.startswith("review-worker_"):
            started.set()
            assert release.wait(2), "hanging start was never released"
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", hanging_start)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    try:
        future = executor.submit(lambda: "ran")
        assert started.wait(1), "dispatcher never reached Thread.start"
        began = time.monotonic()
        executor.shutdown(wait=False)
        assert time.monotonic() - began < 0.2
        assert not future.done()
    finally:
        release.set()
        assert future.result(timeout=2) == "ran"


def test_thread_start_exception_fails_the_future(monkeypatch):
    real_start = threading.Thread.start

    def boom(self, *args, **kwargs):
        if self.name.startswith("review-worker_"):
            raise RuntimeError("thread limit")
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", boom)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    future = executor.submit(lambda: 1)
    try:
        future.result(timeout=2)
    except RuntimeError as exc:
        assert "thread limit" in str(exc)
    else:
        raise AssertionError("start failure was not the future's result")
    executor.shutdown(wait=True)
    assert not [thread for thread in executor._threads if thread.name.startswith("review-worker_")]
    assert executor._inflight == 0


def test_cancel_futures_drops_a_start_that_has_not_returned(monkeypatch):
    """A dequeued future blocked inside Thread.start is not queued and not in ``_threads``.

    ``cancel_futures`` must still drop it. Releasing the start gate afterwards must not run the body.
    """
    entered = threading.Event()
    release = threading.Event()
    ran = threading.Event()
    real_start = _UnboundedThreadExecutor._start_registered

    def gated(self, thread, fut):
        entered.set()
        assert release.wait(2), "start gate was never released"
        return real_start(self, thread, fut)

    monkeypatch.setattr(_UnboundedThreadExecutor, "_start_registered", gated)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    try:
        future = executor.submit(lambda: ran.set())
        assert entered.wait(1), "dispatcher never reached the gated start"
        began = time.monotonic()
        executor.shutdown(wait=False, cancel_futures=True)
        assert time.monotonic() - began < 0.2, "shutdown(wait=False) blocked on Thread.start"
        assert future.cancelled()
        assert len(executor._threads) == 0
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while executor._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor._inflight == 0
    assert not ran.is_set()
    assert future.cancelled()


def test_cancel_futures_drops_work_still_queued(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    ran = threading.Event()
    real_dispatch = _UnboundedThreadExecutor._dispatch

    def gated(self):
        entered.set()
        assert gate.wait(2), "dispatcher gate was never released"
        return real_dispatch(self)

    monkeypatch.setattr(_UnboundedThreadExecutor, "_dispatch", gated)
    executor = _UnboundedThreadExecutor(thread_name_prefix="review-worker")
    future = executor.submit(lambda: ran.set())
    assert entered.wait(1)
    executor.shutdown(wait=False, cancel_futures=True)
    assert future.cancelled()
    gate.set()
    time.sleep(0.05)
    assert not ran.is_set()
