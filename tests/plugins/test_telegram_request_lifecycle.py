"""Real PTB clients must not reuse a closed Telegram fallback transport."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("telegram", reason="real python-telegram-bot required for client lifecycle tests")

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram import telegram_network as tnet
from plugins.platforms.telegram.telegram_io import io_loop


class _WireTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.block_sends = False
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def handle_async_request(self, request):
        # Exercise the hostname pool too: it rejects reuse after transport close.
        if request.url.host != "api.telegram.org":
            raise httpx.ConnectError("fixture IPv4 unavailable")
        if self.block_sends and request.url.path.endswith("/sendMessage"):
            self.send_started.set()
            await self.release_send.wait()
        return httpx.Response(200, json={"ok": True, "result": []}, request=request)

    async def aclose(self):
        pass


@pytest.fixture
def adapter(monkeypatch):
    for name in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy",
        "all_proxy", "TELEGRAM_PROXY", "NO_PROXY", "no_proxy",
        "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tnet, "_build_inner_transport", lambda _kwargs: _WireTransport())
    for name, value in {
        "HERMES_TELEGRAM_HTTP_POOL_SIZE": "17",
        "HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT": "11",
        "HERMES_TELEGRAM_HTTP_READ_TIMEOUT": "23",
        "HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT": "27",
        "HERMES_TELEGRAM_HTTP_POOL_TIMEOUT": "9",
    }.items():
        monkeypatch.setenv(name, value)
    instance = tg_adapter.TelegramAdapter(PlatformConfig(enabled=True, token="123456:fixture"))
    monkeypatch.setattr(instance, "_fallback_ips", lambda: ["149.154.166.110"])
    return instance


async def _request(request, method="getUpdates"):
    status, payload = await request.do_request(f"https://api.telegram.org/botfixture/{method}", "POST")
    assert status == 200
    assert request.parse_json_payload(payload)["ok"] is True


def _assert_pool_contract(general, polling):
    assert general._client._transport is not polling._client._transport
    for request in (general, polling):
        timeout = request._client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (11, 23, 27, 9)
        assert request._client._transport._transport_kwargs["limits"].max_connections == 17
    assert general._client._transport._transport_kwargs["limits"].max_keepalive_connections >= 1
    assert polling._client._transport._transport_kwargs["limits"].max_keepalive_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", ["general", "polling"])
async def test_reconnect_builds_fresh_transport_for_each_client(adapter, pool):
    general, polling = await adapter._build_ptb_requests()
    adapter._app = SimpleNamespace(bot=SimpleNamespace(_request=(polling, general)))
    request = general if pool == "general" else polling
    sibling = polling if pool == "general" else general
    try:
        _assert_pool_contract(general, polling)
        await _request(request)
        await _request(sibling)
        old_client = request._client
        sibling_client = sibling._client
        old_transport = old_client._transport
        for _ in range(2):
            if pool == "polling":
                await adapter._drain_polling_connections()
            else:
                await request.shutdown()
                await request.initialize()
            # Do real I/O before identity checks: the unfixed code fails here.
            await _request(request)
            _assert_pool_contract(general, polling)
            assert request._client._transport is not old_transport
            assert request._client is not old_client
            assert sibling._client is sibling_client
            await _request(sibling)
            old_client = request._client
            old_transport = old_client._transport
    finally:
        await general.shutdown()
        await polling.shutdown()


def _io_set(event):
    io_loop().loop.call_soon_threadsafe(event.set)


async def _await_io_tasks(tasks):
    if not tasks:
        return
    async def wait():
        await asyncio.gather(*tasks, return_exceptions=True)
    await io_loop().run(wait())


@pytest.mark.asyncio
async def test_abandoned_close_cannot_close_replacement_or_interrupt_send(adapter, monkeypatch):
    general, polling = await adapter._build_ptb_requests()
    adapter._app = SimpleNamespace(bot=SimpleNamespace(_request=(polling, general)))
    await _request(general)
    await _request(polling)
    old_client = polling._client
    old_transport = old_client._transport
    general_client = general._client
    send_wire = general_client._transport._primary
    send_wire.block_sends = True
    send = asyncio.create_task(_request(general, "sendMessage"))
    release_close = asyncio.Event()
    close_started = asyncio.Event()
    closers = set()
    original_close = old_client.aclose

    async def delayed_close():
        # Shield the close onto its own I/O task so cancelling the hop
        # cannot busy-loop the transport loop on CancelledError.
        async def body():
            closers.add(asyncio.current_task())
            close_started.set()
            await release_close.wait()
            await original_close()
        task = asyncio.create_task(body())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(old_client, "aclose", delayed_close)
    monkeypatch.setattr(tg_adapter, "_DRAIN_TIMEOUT", 2.0)
    drain = asyncio.create_task(adapter._drain_polling_connections())
    try:
        await asyncio.wait_for(send_wire.send_started.wait(), 5)
        await asyncio.wait_for(close_started.wait(), 5)
        await asyncio.wait_for(drain, 5)
        assert polling._client is not old_client
        _assert_pool_contract(general, polling)
        assert general._client is general_client
        assert not send.done()
        await _request(polling)
        _io_set(release_close)
        await asyncio.wait_for(_await_io_tasks(closers), 5)
        await asyncio.gather(*list(adapter._background_tasks))
        # The late close must touch only the old client's transport.
        await _request(polling)
        assert old_transport._primary_closed
        _assert_pool_contract(general, polling)
        assert polling._client._transport is not old_transport
        _io_set(send_wire.release_send)
        await asyncio.wait_for(send, 5)
    finally:
        _io_set(release_close)
        _io_set(send_wire.release_send)
        await asyncio.gather(drain, send, *list(adapter._background_tasks), return_exceptions=True)
        await _await_io_tasks(closers)
        await general.shutdown()
        await polling.shutdown()


@pytest.mark.asyncio
async def test_orphan_rebuild_closes_client_on_io_loop(adapter, monkeypatch):
    general, polling = await adapter._build_ptb_requests()
    adapter._app = SimpleNamespace(bot=SimpleNamespace(_request=(polling, general)))
    await polling.initialize()
    old = polling._client
    loops = []
    original_close = old.aclose

    async def track_close():
        loops.append(asyncio.get_running_loop())
        await original_close()

    monkeypatch.setattr(old, "aclose", track_close)
    try:
        await adapter._orphan_and_rebuild_polling_client(polling)
        await asyncio.gather(*list(adapter._background_tasks))
        assert loops == [io_loop().loop]
        assert polling._client is not old
    finally:
        await general.shutdown()
        await polling.shutdown()
