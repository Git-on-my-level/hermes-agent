"""Real PTB clients must not reuse a closed Telegram fallback transport."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram import telegram_network as tnet


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
    instance = tg_adapter.TelegramAdapter(PlatformConfig(enabled=True, token="123456:fixture"))
    monkeypatch.setattr(instance, "_fallback_ips", lambda: ["149.154.166.110"])
    return instance


async def _request(request, method="getUpdates"):
    status, payload = await request.do_request(f"https://api.telegram.org/botfixture/{method}", "POST")
    assert status == 200
    assert request.parse_json_payload(payload)["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", ["general", "polling"])
async def test_reconnect_builds_fresh_transport_for_each_client(adapter, pool):
    general, polling = await adapter._build_ptb_requests()
    adapter._app = SimpleNamespace(bot=SimpleNamespace(_request=(polling, general)))
    request = general if pool == "general" else polling
    sibling = polling if pool == "general" else general
    try:
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
            assert request._client._transport is not old_transport
            assert request._client is not old_client
            assert sibling._client is sibling_client
            await _request(sibling)
            old_client = request._client
            old_transport = old_client._transport
    finally:
        await general.shutdown()
        await polling.shutdown()


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
        closers.add(asyncio.current_task())
        close_started.set()
        # Mimic a cancellation-shielded close which outlives the drain deadline.
        while not release_close.is_set():
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                continue
        await original_close()

    monkeypatch.setattr(old_client, "aclose", delayed_close)
    monkeypatch.setattr(tg_adapter, "_DRAIN_TIMEOUT", 2.0)
    drain = asyncio.create_task(adapter._drain_polling_connections())
    try:
        await asyncio.wait_for(send_wire.send_started.wait(), 5)
        await asyncio.wait_for(close_started.wait(), 5)
        await asyncio.wait_for(drain, 5)
        assert polling._client is not old_client
        assert general._client is general_client
        assert not send.done()
        await _request(polling)
        release_close.set()
        await asyncio.wait_for(asyncio.gather(*closers), 5)
        await asyncio.gather(*list(adapter._background_tasks))
        # The late close must touch only the old client's transport.
        await _request(polling)
        assert old_transport._primary_closed
        assert polling._client._transport is not old_transport
        send_wire.release_send.set()
        await asyncio.wait_for(send, 5)
    finally:
        release_close.set()
        send_wire.release_send.set()
        await asyncio.gather(drain, send, *closers, *list(adapter._background_tasks), return_exceptions=True)
        await general.shutdown()
        await polling.shutdown()
