"""A connect cancelled during token-lock acquisition must release its late lock."""
import asyncio
import threading

import pytest

from gateway.adapter_factory import acquire_adapter_lock
from gateway import status
from gateway.status_scoped_locks import release_scoped_lock
from tests.gateway.test_platform_reconnect import StubAdapter


@pytest.mark.asyncio
async def test_teardown_during_blocked_acquisition_releases_late_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    adapter = StubAdapter()
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = status.acquire_scoped_lock
    scope, identity = "telegram-bot-token", "cancelled-connect-token"

    def blocked_acquire(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(10), "test never unblocked acquisition"
        return acquire(*args, **kwargs)

    monkeypatch.setattr(status, "acquire_scoped_lock", blocked_acquire)
    connecting = asyncio.create_task(acquire_adapter_lock(adapter, scope, identity, "test token"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        # Runner teardown clears identity before the worker has acquired anything.
        await asyncio.to_thread(adapter._release_platform_lock)
        assert adapter._platform_lock_identity is None
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*adapter._detached_fatal_tasks), 10)
        assert not status._get_scope_lock_path(scope, identity).exists()
    finally:
        proceed.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await asyncio.gather(*adapter._detached_fatal_tasks, return_exceptions=True)
        release_scoped_lock(scope, identity)
