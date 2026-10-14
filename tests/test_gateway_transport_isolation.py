"""Real localhost Bot API traffic only: no token or external network required."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
pytest.importorskip("telegram")
from telegram.ext import Application
from telegram.request import HTTPXRequest

from gateway.loop_lag import start_loop_lag_watchdog
from plugins.platforms.telegram.telegram_io import isolate_request, isolate_updater, io_loop
from plugins.platforms.telegram.telegram_network import TelegramFallbackTransport


@pytest.fixture
def bot_api():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            method = self.path.rsplit('/', 1)[-1]
            calls.append((method, time.monotonic()))
            result = {'id': 123, 'is_bot': True, 'first_name': 'Fake', 'username': 'fake_bot'}
            if method == 'getUpdates':
                time.sleep(0.02)
                count = sum(method == 'getUpdates' for method, _ in calls)
                result = [] if count % 3 else [{'update_id': count, 'message': {'message_id': count, 'date': 0, 'chat': {'id': 1, 'type': 'private'}, 'text': 'fake'}}]
            elif method == 'sendMessage':
                result = {'message_id': 1, 'date': 0, 'chat': {'id': 1, 'type': 'private'}, 'text': 'fake'}
            elif method != 'getMe':
                result = True
            payload = json.dumps({'ok': True, 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/bot', calls
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.asyncio
async def test_real_poll_and_send_continue_while_gateway_waits_on_rlock(bot_api):
    url, calls = bot_api
    general = isolate_request(HTTPXRequest())
    polling = isolate_request(HTTPXRequest())
    app = Application.builder().token('123:fake').base_url(url).request(general).get_updates_request(polling).build()
    isolate_updater(app)
    await app.initialize()
    await app.updater.start_polling()
    lock = threading.RLock()
    held = threading.Event()
    def holder():
        with lock:
            held.set()
            time.sleep(0.5)
    worker = threading.Thread(target=holder)
    worker.start()
    await asyncio.to_thread(held.wait)
    before = len([c for c in calls if c[0] == 'getUpdates'])
    # Schedule the send directly on the transport loop before reproducing the
    # synchronous agent-lock stall. The fake server proves network progress.
    sent = asyncio.run_coroutine_threadsafe(app.bot.send_message(1, 'fake'), io_loop().loop)
    started = time.monotonic()
    with lock:
        pass
    elapsed = time.monotonic() - started
    assert elapsed > 0.3  # unfixed on-loop lock reproduction
    assert sent.done()
    assert len([c for c in calls if c[0] == 'getUpdates']) > before + 3
    send_time = next(t for method, t in calls if method == 'sendMessage')
    assert send_time - started < 0.3
    await app.updater.stop()
    await app.shutdown()
    worker.join(2)


@pytest.mark.asyncio
async def test_status_rlock_does_not_stall_gateway_or_send(bot_api, monkeypatch, tmp_path):
    from gateway import status
    from gateway.status_async import publish_runtime_status
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    url, calls = bot_api
    req = isolate_request(HTTPXRequest())
    app = Application.builder().token('123:fake').base_url(url).request(req).build()
    await app.initialize()
    held, release = threading.Event(), threading.Event()
    def holder():
        with status._runtime_status_state_lock:
            held.set()
            release.wait(3)
    worker = threading.Thread(target=holder)
    worker.start()
    await asyncio.to_thread(held.wait)
    started = time.monotonic()
    pending = publish_runtime_status(platform='telegram', platform_state='retrying')
    try:
        await asyncio.wait_for(app.bot.send_message(1, 'fake'), 0.5)
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < 0.5
        assert not pending.done()
    finally:
        release.set()
        await pending
        await app.shutdown()
        worker.join(2)


@pytest.mark.asyncio
async def test_watchdog_names_blocked_stack_before_recovery(caplog):
    loop = asyncio.get_running_loop()
    stop = start_loop_lag_watchdog(loop, threshold=0.04, interval=0.01, rate_limit=10)
    try:
        def offending_block():
            time.sleep(0.18)
        offending_block()
        await asyncio.sleep(0.02)
        warnings = [r for r in caplog.records if 'Gateway loop lag' in r.message]
        assert len(warnings) == 1
        assert 'offending_block' in warnings[0].message
    finally:
        stop.set()


@pytest.mark.asyncio
async def test_fallback_closes_every_pool_and_cannot_reopen(monkeypatch):
    transport = TelegramFallbackTransport([])
    closed = []
    class Pool:
        async def aclose(self):
            closed.append(self)
            raise OSError('close failed')
    transport._primary = Pool()
    transport._fallbacks['149.154.166.110'] = Pool()
    await transport.aclose()
    assert len(closed) == 2
    with pytest.raises(RuntimeError, match='closed'):
        await transport._get_fallback('149.154.166.110')


@pytest.mark.asyncio
async def test_primary_reconnects_release_real_sockets(bot_api):
    url, _ = bot_api
    import psutil
    process = psutil.Process()
    port = int(url.split(':')[2].split('/')[0])
    def sockets():
        return [c for c in process.net_connections(kind='tcp') if c.raddr and c.raddr.port == port]
    transport = TelegramFallbackTransport([], proxy=None)
    client = httpx.AsyncClient(transport=transport)
    try:
        for _ in range(12):
            response = await client.post(url + '123:fake/getMe')
            assert response.status_code == 200
            primary = transport._primary
            assert sockets()
            await transport._reset_primary(primary)
            assert not sockets()
    finally:
        await client.aclose()
    assert not sockets()


@pytest.mark.asyncio
async def test_console_rlock_is_never_acquired_by_loop():
    import io
    import logging
    from gateway.queued_console import QueuedConsoleHandler
    sink = io.StringIO()
    handler = QueuedConsoleHandler(sink)
    held, release = threading.Event(), threading.Event()
    def holder():
        with handler.lock:
            held.set()
            release.wait(2)
    worker = threading.Thread(target=holder)
    worker.start()
    await asyncio.to_thread(held.wait)
    started = time.monotonic()
    try:
        handler.handle(logging.LogRecord('test', logging.WARNING, __file__, 0, 'queued', (), None))
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < 0.2
    finally:
        release.set()
        worker.join(2)
        await asyncio.to_thread(handler.close)
    assert 'queued' in sink.getvalue()


@pytest.mark.asyncio
async def test_read_failure_closes_fallback_sockets_without_resending(bot_api, monkeypatch):
    import plugins.platforms.telegram.telegram_network as network
    import psutil
    url, calls = bot_api
    port = int(url.split(':')[2].split('/')[0])
    instances = []
    class FailingPool(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()
            instances.append(self)
            self.closed = False
        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(httpx.Request('POST', url + '123:fake/getMe'))
            await response.aread()
            await response.aclose()
            raise httpx.ReadError('peer failed after accepting request')
        async def aclose(self):
            await self.inner.aclose()
            self.closed = True
    monkeypatch.setattr(network, '_build_inner_transport', lambda _: FailingPool())
    monkeypatch.setattr(network, '_resolve_proxy_url', lambda **_: None)
    transport = TelegramFallbackTransport(['149.154.166.110', '149.154.167.220'])
    process = psutil.Process()
    try:
        for _ in range(10):
            with pytest.raises(httpx.ReadError):
                await transport.handle_async_request(httpx.Request('POST', 'https://api.telegram.org/bot123:fake/sendMessage'))
            assert instances[-1].closed
            assert not [c for c in process.net_connections(kind='tcp') if c.raddr and c.raddr.port == port]
        assert len(calls) == 10  # Never retry an ambiguously accepted message.
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_shutdown_reinitialize_keeps_transport_on_its_loop(bot_api):
    url, calls = bot_api
    request = isolate_request(HTTPXRequest())
    for _ in range(5):
        await request.initialize()
        status, _ = await request.do_request(url + '123:fake/getMe', 'POST')
        assert status == 200
        await request.shutdown()
        assert request._client.is_closed
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_adapter_connect_disconnect_uses_real_local_bot_api(bot_api, monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    import plugins.platforms.telegram.adapter as adapter_module
    url, calls = bot_api
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_TELEGRAM_DISABLE_FALLBACK_IPS', 'true')
    monkeypatch.setattr(adapter_module, 'resolve_proxy_url', lambda *_, **__: None)
    monkeypatch.setattr(adapter_module, '_get_scoped_secret', lambda *_: None)
    adapter = adapter_module.TelegramAdapter(PlatformConfig(
        enabled=True, token='123:fake', extra={'base_url': url, 'base_file_url': url},
    ))
    try:
        assert await adapter.connect()
        assert any(method == 'getUpdates' for method, _ in calls)
        assert adapter._polling_progress_event.is_set()
        assert adapter._send_path_degraded is False
    finally:
        await adapter.disconnect()
    assert not adapter._running


@pytest.mark.asyncio
async def test_coalesced_status_readd_follows_profile_drop(monkeypatch):
    from gateway import status
    from gateway.status_async import publish_runtime_status
    calls = []
    monkeypatch.setattr(status, 'publish_runtime_status', lambda **fields: calls.append(fields))
    publish_runtime_status(platform='alpha:telegram', platform_state='retrying')
    publish_runtime_status(drop_profile_platforms='alpha')
    settled = publish_runtime_status(platform='alpha:telegram', platform_state='connected')
    await settled
    assert calls == [
        {'drop_profile_platforms': 'alpha'},
        {'platform': 'alpha:telegram', 'platform_state': 'connected'},
    ]


@pytest.mark.asyncio
async def test_agent_diagnostic_lock_does_not_block_transport_loop(monkeypatch):
    import plugins.platforms.telegram.telegram_network as network
    lock = threading.Lock()
    entered = threading.Event()
    closed = []
    def describe(error):
        entered.set()
        with lock:
            return 'mock-redacted-error'
    class Pool(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectTimeout('fake')
        async def aclose(self):
            closed.append(self)
    monkeypatch.setattr(network, '_describe_transport_error', describe)
    monkeypatch.setattr(network, '_build_inner_transport', lambda _: Pool())
    monkeypatch.setattr(network, '_resolve_proxy_url', lambda **_: None)
    transport = TelegramFallbackTransport(['149.154.166.110'])
    with lock:
        request = asyncio.create_task(io_loop().run(transport.handle_async_request(
            httpx.Request('POST', 'https://api.telegram.org/bot123:fake/getUpdates'))))
        assert await asyncio.to_thread(entered.wait, 2)
        assert closed  # Failed pool released before contended diagnostics.
        await asyncio.wait_for(io_loop().run(asyncio.sleep(0.01)), 0.3)
        assert not request.done()
    with pytest.raises(httpx.ConnectTimeout):
        await request
    await io_loop().run(transport.aclose())


@pytest.mark.asyncio
async def test_conflict_disarms_retry_before_gateway_callback(monkeypatch):
    from telegram.ext import Updater
    from telegram.error import Conflict
    owner = threading.get_ident()
    seen = []
    async def start(self, **kwargs):
        self._running = True
        event = getattr(self, '_Updater__polling_task_stop_event')
        kwargs['error_callback'](Conflict('fake conflict'))
        assert event.is_set()
    async def stop(self):
        self._running = False
    monkeypatch.setattr(Updater, 'start_polling', start)
    monkeypatch.setattr(Updater, 'stop', stop)
    app = Application.builder().token('123:fake').build()
    isolate_updater(app)
    try:
        await app.updater.start_polling(error_callback=lambda _: seen.append(threading.get_ident()))
        await asyncio.sleep(0)
        assert seen == [owner]
        await app.updater.stop()
    finally:
        for request in app.bot._request:
            await request.shutdown()


@pytest.mark.asyncio
async def test_webhook_keeps_gateway_owned_queue(monkeypatch):
    from telegram.ext import Updater
    owner = threading.get_ident()
    app = Application.builder().token('123:fake').build()
    async def start(self, **kwargs):
        assert threading.get_ident() == owner
        assert self.update_queue is original
        await self.update_queue.put('webhook')
    monkeypatch.setattr(Updater, 'start_webhook', start)
    isolate_updater(app)
    original = app.update_queue
    try:
        await app.updater.start_webhook()
        assert await original.get() == 'webhook'
    finally:
        for request in app.bot._request:
            await request.shutdown()


@pytest.mark.asyncio
async def test_handoff_is_durable_and_stop_does_not_wait_for_dispatch(monkeypatch):
    from telegram import Update
    from telegram.ext import Updater
    async def start(self, **kwargs):
        self._running = True
    async def stop(self):
        self._running = False
    monkeypatch.setattr(Updater, 'start_polling', start)
    monkeypatch.setattr(Updater, 'stop', stop)
    app = Application.builder().token('123:fake').build()
    isolate_updater(app)
    try:
        await app.updater.start_polling()
        bridge = app.updater.update_queue
        with pytest.raises(RuntimeError):
            await app.updater.start_polling()
        with pytest.raises(RuntimeError):
            await app.updater.start_webhook()
        async def produce():
            for item in range(300):
                await bridge.put(Update(item))
        await asyncio.wait_for(io_loop().run(produce()), 10)
        assert app.update_queue.qsize() <= 256
        assert await io_loop().build(app.updater.durable_inbox.count) == 300
        await asyncio.wait_for(app.updater.stop(), 1)
        # No dispatcher drain: every accepted update is still stored.
        rows = await io_loop().build(app.updater.durable_inbox.after, 0, 400)
        assert [json.loads(row[1])['update_id'] for row in rows] == list(range(300))
    finally:
        for request in app.bot._request:
            await request.shutdown()


@pytest.mark.asyncio
async def test_cancelling_gateway_waiter_cancels_io_task():
    started, cancelled = threading.Event(), threading.Event()
    async def request():
        started.set()
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.set()
    waiting = asyncio.create_task(io_loop().run(request()))
    assert await asyncio.to_thread(started.wait, 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await asyncio.to_thread(cancelled.wait, 1)


@pytest.mark.asyncio
async def test_abandoned_poll_client_rebuild_and_close_use_owner_loop(monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
    request = isolate_request(HTTPXRequest())
    await request.shutdown()
    owner = threading.get_ident()
    closed = []
    built = []
    class Client:
        is_closed = False
        async def aclose(self):
            closed.append(threading.get_ident())
            self.is_closed = True
    old = Client()
    request._client = old
    def build(_):
        built.append(threading.get_ident())
        return Client()
    monkeypatch.setattr(type(request), '_build_client', build)
    await adapter._orphan_and_rebuild_polling_client(request)
    assert request._client is not old
    await asyncio.gather(*tuple(adapter._background_tasks))
    assert old.is_closed
    assert closed == [io_loop().thread.ident]
    assert built and built[0] != owner
    await request.shutdown()


@pytest.mark.asyncio
async def test_orphan_rebuilds_do_not_accumulate_real_tcp_sockets(bot_api, monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    import psutil
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
    request = isolate_request(HTTPXRequest())
    url, _ = bot_api
    port = int(url.split(':')[2].split('/')[0])
    process = psutil.Process()
    try:
        for _ in range(10):
            status, _ = await request.do_request(url + '123:fake/getMe', 'POST')
            assert status == 200
            old = request._client
            await adapter._orphan_and_rebuild_polling_client(request)
            await asyncio.gather(*tuple(adapter._background_tasks))
            assert old.is_closed
            assert not [c for c in process.net_connections(kind='tcp') if c.raddr and c.raddr.port == port]
    finally:
        await request.shutdown()
