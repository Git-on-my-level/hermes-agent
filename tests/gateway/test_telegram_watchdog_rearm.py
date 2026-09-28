"""Outer-ring Telegram watchdog supervisor (gateway housekeeping re-arm).

Production failure (m4-mac-mini, 2026-09-27/28): a transient ``ConnectError`` at the
long-poll killed the polling task tree *including* ``_polling_heartbeat_loop`` — the
watchdog that exists to catch exactly this class (#92991/#55769: poller dead, transport
healthy, queue empty). With the watchdog gone, nothing logged and nothing retried: the
gateway stayed alive but deaf until an external restart, three times in one day.

Two regressions:

* adapter: the heartbeat's stand-down return (teardown/fatal) was silent, so a LOST
  supervisor handoff was indistinguishable from a healthy adapter — it now logs once.
* gateway: ``_housekeeping_telegram_watchdog_rearm`` (housekeeping thread, outside the
  adapter task tree) re-arms a dead heartbeat so the normal stall/queue detectors run
  again and escalate through the recovery ladder. No-op while alive; never touches
  webhook-mode, torn-down, fatal, or not-yet-connected adapters. Walks every served
  profile's adapter map, not only the launch profile.
"""
import asyncio
import contextlib
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import (
    _housekeeping_telegram_watchdog_rearm,
    _telegram_watchdog_rearm_once,
)
from plugins.platforms.telegram.adapter import TelegramAdapter


async def _cancel_heartbeat(adapter) -> None:
    """Cancel the spawned heartbeat and let the cancellation land (house style)."""
    task = adapter._polling_heartbeat_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    adapter._polling_heartbeat_task = None


def _polling_adapter() -> TelegramAdapter:
    """Polling-mode adapter with no heartbeat running (post-loss connected state)."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._webhook_mode = False
    adapter._polling_heartbeat_task = None
    adapter._running = True
    return adapter


@pytest.mark.asyncio
async def test_rearm_spawns_heartbeat_when_missing(caplog):
    """Dead/missing heartbeat while in polling mode is re-armed and reported."""
    adapter = _polling_adapter()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        changed = _telegram_watchdog_rearm_once(adapter)
    assert changed is True
    task = adapter._polling_heartbeat_task
    assert task is not None and not task.done()
    assert any("re-arming from gateway housekeeping" in r.message for r in caplog.records)
    await _cancel_heartbeat(adapter)


@pytest.mark.asyncio
async def test_rearm_replaces_done_task_left_behind(caplog):
    """A finished heartbeat task stored on the adapter is replaced, not kept."""
    adapter = _polling_adapter()

    async def _already_done():
        return None

    stale = asyncio.ensure_future(_already_done())
    await stale
    adapter._polling_heartbeat_task = stale
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        changed = _telegram_watchdog_rearm_once(adapter)
    assert changed is True
    task = adapter._polling_heartbeat_task
    assert task is not stale and not task.done()
    await _cancel_heartbeat(adapter)


@pytest.mark.asyncio
async def test_rearm_noop_while_heartbeat_alive():
    """A live heartbeat must never be cancelled/replaced by the outer-ring check."""
    adapter = _polling_adapter()

    async def _eternal():
        await asyncio.sleep(3600)

    live = asyncio.ensure_future(_eternal())
    adapter._polling_heartbeat_task = live
    changed = _telegram_watchdog_rearm_once(adapter)
    assert changed is False
    assert adapter._polling_heartbeat_task is live
    live.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await live
    adapter._polling_heartbeat_task = None


@pytest.mark.parametrize("mode", ["webhook", "teardown", "fatal"])
def test_rearm_skips_non_polling_or_retired_adapters(mode):
    """Webhook-mode, torn-down, and fatal adapters are none of the re-arm's business."""
    adapter = _polling_adapter()
    if mode == "webhook":
        adapter._webhook_mode = True
    elif mode == "teardown":
        adapter._polling_teardown_started = True
    else:
        adapter._set_fatal_error("telegram_network_error", "retryable test fatal", retryable=True)
    changed = _telegram_watchdog_rearm_once(adapter)
    assert changed is False
    assert adapter._polling_heartbeat_task is None


def test_rearm_skips_while_not_connected():
    """``connect()`` clears webhook/teardown before ``_running``; mid-connect must not grow a watchdog."""
    adapter = _polling_adapter()
    adapter._running = False
    changed = _telegram_watchdog_rearm_once(adapter)
    assert changed is False
    assert adapter._polling_heartbeat_task is None


@pytest.mark.asyncio
async def test_heartbeat_standdown_logs_once(monkeypatch, caplog):
    """The stand-down return must log once, not silently (lost-handoff visibility)."""
    adapter = _polling_adapter()
    adapter._polling_teardown_started = True
    monkeypatch.setattr(adapter, "_check_polling_stall", AsyncMock())
    monkeypatch.setattr(adapter, "_check_ingress_dispatch_stall", lambda: None)
    # Instant sleep: the heartbeat's real first wait is 90s (house style from
    # test_telegram_conflict.py — an unpatched sleep starves the 5s timeout).
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
        await asyncio.wait_for(adapter._polling_heartbeat_loop(), timeout=5)
    warnings = [r for r in caplog.records if "standing down" in r.message]
    assert len(warnings) == 1
    assert adapter._heartbeat_standdown_logged is True


def test_housekeeping_chore_rearms_from_outside_the_loop():
    """The chore schedules the re-arm on the gateway loop from the housekeeping thread.

    Mirrors production topology: chore runs in the housekeeping thread, the adapter's
    task tree lives on the gateway loop; non-telegram platforms are ignored.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    adapter = None
    try:
        adapter = _polling_adapter()
        adapters = {Platform.TELEGRAM: adapter, Platform.SLACK: object()}
        _housekeeping_telegram_watchdog_rearm(adapters, loop)
        task = adapter._polling_heartbeat_task
        assert task is not None and not task.done()
    finally:
        if adapter is not None and adapter._polling_heartbeat_task is not None:
            task = adapter._polling_heartbeat_task

            def _stop_when_done(_t=None):
                loop.call_soon_threadsafe(loop.stop)

            # Stop only after the cancellation has been processed, so no
            # pending task survives to loop.close().
            task.add_done_callback(_stop_when_done)
            loop.call_soon_threadsafe(task.cancel)
        else:
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_housekeeping_chore_noop_without_adapters_or_loop():
    """Missing adapters/loop (external providers) is a safe no-op."""
    _housekeeping_telegram_watchdog_rearm(None, None)
    _housekeeping_telegram_watchdog_rearm({}, None)


def test_housekeeping_chore_rearms_secondary_profile_adapters():
    """Multiplex secondaries live in ``runner._profile_adapters``, not the launch map."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    primary = secondary = None
    try:
        primary = _polling_adapter()
        secondary = _polling_adapter()
        runner = SimpleNamespace(_profile_adapters={"coder": {Platform.TELEGRAM: secondary}})
        _housekeeping_telegram_watchdog_rearm({Platform.TELEGRAM: primary}, loop, runner)
        for adapter in (primary, secondary):
            task = adapter._polling_heartbeat_task
            assert task is not None and not task.done()
    finally:
        tasks = []
        for adapter in (primary, secondary):
            if adapter is not None and adapter._polling_heartbeat_task is not None:
                tasks.append(adapter._polling_heartbeat_task)
        pending = {t for t in tasks if not t.done()}
        if not pending:
            loop.call_soon_threadsafe(loop.stop)
        else:
            def _stop_when_idle(_t=None):
                pending.discard(_t)
                if not pending:
                    loop.call_soon_threadsafe(loop.stop)

            for task in list(pending):
                task.add_done_callback(_stop_when_idle)
                loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=5)
        loop.close()


def test_schedule_recovery_logs_breadcrumb_when_fatal_suppressed(caplog):
    """A fatal-suppressed degradation must emit the stranded-adapter breadcrumb."""
    adapter = _polling_adapter()
    adapter._set_fatal_error("telegram_network_error", "retryable fatal", retryable=True)
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
        adapter._schedule_polling_recovery(RuntimeError("blip"), reason="test probe")
    assert any("recovery suppressed" in r.message for r in caplog.records)
    assert adapter._polling_error_task is None
