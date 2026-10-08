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
