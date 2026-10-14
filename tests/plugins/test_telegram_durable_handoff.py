"""Saturation and stop retain all offset-acknowledged polling input."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('telegram')
from telegram import Update
from telegram.ext import Application, Updater
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter, _POLLING_STALL_TIMEOUT
from plugins.platforms.telegram.telegram_io import isolate_updater, io_loop


async def make_app(monkeypatch):
    async def start(self, **kwargs):
        self._running = True
    async def stop(self):
        self._running = False
    monkeypatch.setattr(Updater, 'start_polling', start)
    monkeypatch.setattr(Updater, 'stop', stop)
    app = Application.builder().token('123:fake').build()
    isolate_updater(app)
    await app.updater.start_polling()
    return app


async def close(app):
    if app.updater.running:
        await app.updater.stop()
    for request in app.bot._request:
        await request.shutdown()


async def fill(app, count=600):
    async def produce():
        for uid in range(count):
            await app.updater.update_queue.put(Update(uid))
    await asyncio.wait_for(io_loop().run(produce()), 60)
    for _ in range(200):
        if app.update_queue.full():
            break
        await asyncio.sleep(0.01)
    assert app.update_queue.qsize() == 256


@pytest.mark.asyncio
async def test_saturated_beyond_stall_deadline_keeps_all_acknowledged_updates(monkeypatch):
    app = await make_app(monkeypatch)
    try:
        await fill(app)
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
        adapter._app = app
        adapter._polling_last_progress_monotonic = 0
        adapter._schedule_polling_recovery = Mock()
        adapter._updates_received_total = 600
        adapter._updates_dispatched_total = 0
        monkeypatch.setattr('plugins.platforms.telegram.adapter.time', SimpleNamespace(monotonic=lambda: _POLLING_STALL_TIMEOUT + 151))
        await adapter._check_polling_stall()
        for _ in range(20):
            adapter._check_ingress_dispatch_stall()
        adapter._schedule_polling_recovery.assert_not_called()
        assert await io_loop().build(app.updater.durable_inbox.count) == 600
        rows = await io_loop().build(app.updater.durable_inbox.after, 0, 601)
        assert [json.loads(row[1])['update_id'] for row in rows] == list(range(600))
    finally:
        await close(app)


@pytest.mark.asyncio
async def test_stop_saturated_handoff_replays_every_update_on_replacement(monkeypatch):
    app = await make_app(monkeypatch)
    replacement = None
    try:
        await fill(app)
        await asyncio.wait_for(app.updater.stop(), 1)
        replacement = await make_app(monkeypatch)
        # Simulate a replacement process: all input is decoded from committed
        # rows, not transferred from the old queue or its suspended pump.
        updates = []
        for _ in range(600):
            updates.append(await asyncio.wait_for(replacement.update_queue.get(), 3))
        assert [item.update_id for item in updates] == list(range(600))
        assert await io_loop().build(replacement.updater.durable_inbox.count) == 600
    finally:
        await close(app)
        if replacement is not None:
            await close(replacement)


@pytest.mark.asyncio
async def test_commit_failure_does_not_accept_or_enqueue_update(monkeypatch):
    app = await make_app(monkeypatch)
    try:
        def fail(item):
            raise OSError('disk full')
        monkeypatch.setattr(app.updater.durable_inbox, 'put', fail)
        with pytest.raises(OSError, match='disk full'):
            await io_loop().run(app.updater.update_queue.put(Update(123)))
        assert app.update_queue.empty()
        assert await io_loop().build(app.updater.durable_inbox.count) == 0
    finally:
        await close(app)


class PollServer:
    """Bot API offset semantics through PTB's real polling action and stop."""
    def __init__(self):
        self.acknowledged = 0
        self.offsets = []
        self.inbox = None
        self.processed = set()
        self.webhook_probes = 0

    def request(self):
        from telegram.request import BaseRequest
        server = self
        class Request(BaseRequest):
            @property
            def read_timeout(self):
                return 1
            async def initialize(self):
                pass
            async def shutdown(self):
                pass
            async def do_request(self, url, method, request_data=None, **kwargs):
                endpoint = url.rsplit('/', 1)[-1]
                if endpoint == 'getMe':
                    result = {'id': 123, 'is_bot': True, 'first_name': 'Offline'}
                elif endpoint == 'getWebhookInfo':
                    server.webhook_probes += 1
                    result = {'url': '', 'has_custom_certificate': False, 'pending_update_count': 600 - server.acknowledged}
                elif endpoint == 'deleteWebhook':
                    result = True
                else:
                    assert endpoint == 'getUpdates', 'No real sends or other Bot API calls'
                    parameters = request_data.parameters
                    offset = parameters.get('offset', 0)
                    server.offsets.append(offset)
                    if offset:
                        rows = await asyncio.to_thread(server.inbox.after, 0, 1000)
                        stored = {json.loads(row[1])['update_id'] for row in rows}
                        # Every ID acknowledged to the fake Telegram server
                        # must already have committed on local durable storage.
                        assert set(range(offset)) <= stored | server.processed
                    server.acknowledged = max(server.acknowledged, offset)
                    start = server.acknowledged
                    result = [{'update_id': uid} for uid in range(start, min(start + 100, 600))]
                    if not result and parameters.get('timeout', 0):
                        await asyncio.sleep(100)  # The real stop must cancel this polling action.
                return 200, json.dumps({'ok': True, 'result': result}).encode()
        return Request()


async def real_app(server, prepare=None):
    app = Application.builder().token('123:fake').request(server.request()).get_updates_request(server.request()).concurrent_updates(32).build()
    isolate_updater(app)
    server.inbox = app.updater.durable_inbox
    await app.initialize()
    if prepare is not None:
        prepare(app)
    await app.updater.start_polling(drop_pending_updates=False, timeout=10, poll_interval=0)
    return app


async def wait_ack(server, amount):
    for _ in range(6000):
        if server.acknowledged >= amount:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f'offset stalled at {server.acknowledged}')


@pytest.mark.asyncio
async def test_real_polling_acknowledges_only_committed_input_while_saturated(monkeypatch):
    server = PollServer()
    app = await real_app(server)
    try:
        await wait_ack(server, 600)
        assert app.update_queue.full()
        assert (await app.bot.get_me()).id == 123  # General socket is healthy.
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
        adapter._app = app
        adapter._polling_last_progress_monotonic = 0
        adapter._schedule_polling_recovery = Mock()
        monkeypatch.setattr('plugins.platforms.telegram.adapter.time', SimpleNamespace(monotonic=lambda: _POLLING_STALL_TIMEOUT + 151))
        await adapter._check_polling_stall()
        adapter._schedule_polling_recovery.assert_not_called()
        await asyncio.wait_for(app.updater.stop(), 2)
        assert await io_loop().build(server.inbox.count) == 600
    finally:
        await close(app)


@pytest.mark.asyncio
async def test_real_stop_cancels_blocked_commit_without_acknowledging_batch_tail(monkeypatch):
    import threading
    server = PollServer()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    replacement = None
    original_put = None
    def blocked(item):
        if item.update_id == 500:
            entered.set()
            release.wait(120)
            try:
                return original_put(item)
            finally:
                finished.set()
        return original_put(item)
    def prepare(app):
        nonlocal original_put
        original_put = server.inbox.put
        monkeypatch.setattr(server.inbox, 'put', blocked)
    app = await real_app(server, prepare)
    try:
        assert await asyncio.to_thread(entered.wait, 60)
        assert server.acknowledged == 500 and app.update_queue.full()
        await asyncio.wait_for(app.updater.stop(), 2)
        assert server.acknowledged == 500  # Partial 500..599 batch was never acknowledged.
        assert await io_loop().build(server.inbox.count) == 500
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        replacement = await real_app(server)
        await wait_ack(server, 600)
        received = [await asyncio.wait_for(replacement.update_queue.get(), 3) for _ in range(600)]
        assert [item.update_id for item in received] == list(range(600))
        assert await io_loop().build(server.inbox.count) == 600
    finally:
        release.set()
        await close(app)
        if replacement is not None:
            await close(replacement)


@pytest.mark.asyncio
async def test_processed_updates_retire_and_unfinished_updates_survive(monkeypatch):
    from telegram.ext import TypeHandler
    from plugins.platforms.telegram.update_admission import TelegramApplication
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
    app = Application.builder().token('123:fake').application_class(TelegramApplication, {'adapter': adapter}).build()
    isolate_updater(app)
    adapter._app = app
    # Only bot initialization: dispatch and receipt retirement are real, no sends.
    from telegram import User
    app.bot._bot_user = User(id=123, first_name='Offline', is_bot=True)
    app._initialized = True
    processed = []
    async def handler(update, context):
        processed.append(update.update_id)
    app.add_handler(TypeHandler(Update, handler))
    try:
        for uid in range(3):
            await io_loop().build(app.updater.durable_inbox.put, Update(uid))
        await app.process_update(Update(0))
        await app.process_update(Update(1))
        await adapter._update_receipt_flush
        assert processed == [0, 1]
        rows = await io_loop().build(app.updater.durable_inbox.after, 0)
        assert [json.loads(row[1])['update_id'] for row in rows] == [2]
        # A duplicate completed record left by a crash after receipt persistence
        # is retired on replay, without repeating effects.
        await io_loop().build(app.updater.durable_inbox.put, Update(0))
        await app.process_update(Update(0))
        assert processed == [0, 1]
        assert await io_loop().build(app.updater.durable_inbox.count) == 1
    finally:
        await close(app)


@pytest.mark.asyncio
async def test_inbox_home_capture_survives_profile_a_b_a(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    homes = [tmp_path / 'a', tmp_path / 'b', tmp_path / 'a']
    apps = []
    try:
        for home in homes:
            token = set_hermes_home_override(home)
            try:
                apps.append(await make_app(monkeypatch))
            finally:
                reset_hermes_home_override(token)
        await io_loop().run(apps[0].updater.update_queue.put(Update(11)))
        await io_loop().run(apps[1].updater.update_queue.put(Update(22)))
        a = await asyncio.wait_for(apps[2].update_queue.get(), 3)
        b = await asyncio.wait_for(apps[1].update_queue.get(), 3)
        assert a.update_id == 11 and b.update_id == 22
        assert apps[0].updater.durable_inbox.path == apps[2].updater.durable_inbox.path
        assert apps[0].updater.durable_inbox.path != apps[1].updater.durable_inbox.path
    finally:
        for app in apps:
            await close(app)


@pytest.mark.asyncio
async def test_late_commit_from_cancelled_put_wakes_replacement(monkeypatch):
    import threading
    app = await make_app(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    replacement = None
    original_put = app.updater.durable_inbox.put
    def blocked(item):
        entered.set()
        release.wait(10)
        original_put(item)
    monkeypatch.setattr(app.updater.durable_inbox, 'put', blocked)
    sending = asyncio.create_task(io_loop().run(app.updater.update_queue.put(Update(777))))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await asyncio.wait_for(app.updater.stop(), 1)
        replacement = await make_app(monkeypatch)
        # Let the replacement's initial empty-inbox read settle before the
        # old cancelled waiter's write commits. No new Telegram update wakes it.
        assert await io_loop().build(replacement.updater.durable_inbox.count) == 0
        await asyncio.sleep(0.03)
        release.set()
        received = await asyncio.wait_for(replacement.update_queue.get(), 3)
        assert received.update_id == 777
        assert await io_loop().build(replacement.updater.durable_inbox.count) == 1
    finally:
        release.set()
        if not sending.done():
            sending.cancel()
        await close(app)
        if replacement is not None:
            await close(replacement)


@pytest.mark.asyncio
async def test_old_bot_completion_cannot_retire_new_bot_input():
    from plugins.platforms.telegram.update_admission import _record_receipt
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake'))
    replacement = Application.builder().token('456:fake').build()
    isolate_updater(replacement)
    adapter._app = replacement
    try:
        await io_loop().build(replacement.updater.durable_inbox.put, Update(11))
        # An old Application finishes after its adapter has switched bot token.
        _record_receipt(adapter, '123:11')
        await adapter._update_receipt_flush
        assert await io_loop().build(replacement.updater.durable_inbox.count) == 1
    finally:
        await close(replacement)


@pytest.mark.asyncio
async def test_heartbeat_keeps_capped_put_and_alerts_until_consumer_resumes(monkeypatch, caplog):
    server = PollServer()
    app = await real_app(server, lambda app: setattr(app.updater.durable_inbox, 'max_updates', 300))
    from telegram.ext import TypeHandler
    released = asyncio.Event()
    processed = []

    async def wedged_handler(update, context):
        await released.wait()
        processed.append(update.update_id)
        server.processed.add(update.update_id)
        await app.updater.complete_updates([update.update_id])

    app.add_handler(TypeHandler(Update, wedged_handler))
    await app.start()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123:fake', extra={'backpressure_stall_seconds': 300}))
    adapter._app = app
    adapter._schedule_polling_recovery = Mock()
    adapter._write_runtime_status_safe = Mock()
    clock = [0]
    monkeypatch.setattr('plugins.platforms.telegram.adapter.time', SimpleNamespace(monotonic=lambda: clock[0]))
    from unittest.mock import AsyncMock
    stop = AsyncMock(wraps=app.updater.stop)
    monkeypatch.setattr(app.updater, 'stop', stop)
    ticks, heartbeat_boundaries = asyncio.Queue(), asyncio.Queue()
    real_sleep = asyncio.sleep

    async def heartbeat_sleep(delay):
        if delay != 90:
            return await real_sleep(delay)
        heartbeat_boundaries.put_nowait(clock[0])
        await ticks.get()
        clock[0] += 90

    heartbeat_asyncio = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    heartbeat_asyncio.sleep = heartbeat_sleep
    monkeypatch.setattr('plugins.platforms.telegram.adapter.asyncio', heartbeat_asyncio)
    alerts = []
    adapter._write_runtime_status_safe.side_effect = lambda *args, **kwargs: alerts.append(clock[0])
    heartbeat = None
    try:
        await wait_ack(server, 300)
        for _ in range(2000):
            if app.update_processor.current_concurrent_updates == 32 and app.updater.polling_backpressured():
                break
            await asyncio.sleep(0.01)
        assert app.updater.polling_backpressured()
        assert app.update_processor.current_concurrent_updates == 32
        assert processed == []
        assert (await app.bot.get_me()).id == 123
        polling_task = app.updater._Updater__polling_task
        heartbeat = asyncio.create_task(adapter._polling_heartbeat_loop())
        assert await asyncio.wait_for(heartbeat_boundaries.get(), 15) == 0
        # Real heartbeat order: getMe -> getWebhookInfo/pending probe -> stall
        # checks, for 13.5 minutes of pending server input and a capped inbox.
        for sample in range(1, 10):
            ticks.put_nowait(None)
            assert await asyncio.wait_for(heartbeat_boundaries.get(), 15) == sample * 90
            assert app.updater._Updater__polling_task is polling_task
            assert polling_task.cancelling() == 0 and not polling_task.done()
            assert not adapter._recovery_in_flight()
        assert server.webhook_probes == 9
        assert alerts == [450, 810]  # 300s threshold/rate limit sampled every 90s.
        assert adapter._write_runtime_status_safe.call_args.kwargs['needs_attention'] is True
        assert adapter._write_runtime_status_safe.call_args.kwargs['error_code'] == 'telegram_consumer_stalled'
        assert sum(record.levelname == 'ERROR' and 'no completion progress' in record.message for record in caplog.records) == 2
        stop.assert_not_awaited()
        adapter._schedule_polling_recovery.assert_not_called()
        # Exercise the final escalation guard independently of the probe guard.
        adapter._escalate_stuck_consumer('[%s] stale probe', 'stale pending sample')
        assert not adapter._recovery_in_flight()
        assert alerts == [450, 810]
        assert await io_loop().run_inbox(server.inbox.count) == 300
        assert max(server.offsets) == 300
        # Unblock the consumer. Receipts/completion retire capacity only after
        # processing; the partial batch resumes without losing any input.
        released.set()
        await wait_ack(server, 600)
        for _ in range(500):
            if len(processed) == 600 and await io_loop().run_inbox(server.inbox.count) == 0:
                break
            await asyncio.sleep(0.01)
        assert sorted(processed) == list(range(600))
        assert await io_loop().run_inbox(server.inbox.count) == 0
        ticks.put_nowait(None)
        assert await asyncio.wait_for(heartbeat_boundaries.get(), 15) == 900
        assert adapter._pressure_since is None
        assert alerts == [450, 810]
        stop.assert_not_awaited()
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await heartbeat
        released.set()
        await close(app)
        await app.stop()


def test_inbox_limits_retain_old_rows_and_migrate_legacy(tmp_path, monkeypatch):
    import sqlite3
    from plugins.platforms.telegram.update_inbox import UpdateInbox
    inbox = UpdateInbox(tmp_path, '123', max_updates=2)
    with sqlite3.connect(inbox.path) as connection:
        connection.execute('CREATE TABLE updates (seq INTEGER PRIMARY KEY AUTOINCREMENT, update_id TEXT UNIQUE, payload TEXT, kind TEXT)')
        connection.execute('INSERT INTO updates(update_id, payload, kind) VALUES (?, ?, ?)', ('1', '{"update_id":1}', 'update'))
    assert inbox.put(Update(2))
    assert inbox.put(Update(3)) is False
    assert inbox.put(Update(2))
    assert inbox.count() == 2
    assert inbox.complete([1]) == 1
    assert inbox.put(Update(3))

    inbox = UpdateInbox(tmp_path / 'aged', '123', max_updates=2, max_age_seconds=60)
    clock = [1000]
    monkeypatch.setattr('plugins.platforms.telegram.update_inbox.time.time', lambda: clock[0])
    assert inbox.put(Update(1))
    clock[0] += 61
    assert inbox.put(Update(1))  # Existing prefix of a redelivered partial batch.
    assert inbox.put(Update(2)) is False
    assert inbox.count() == 1
    assert inbox.complete([1]) == 1
    assert inbox.put(Update(2))


@pytest.mark.platforms("linux", "macos")
def test_inbox_and_sidecars_private_during_transaction_and_after_kill(tmp_path):
    import os
    import subprocess
    import sys
    from plugins.platforms.telegram.update_inbox import UpdateInbox
    inbox = UpdateInbox(tmp_path, '123')
    assert inbox.put(Update(1))
    os.chmod(inbox.path, 0o644)
    assert inbox.put(Update(2))
    assert os.stat(inbox.path).st_mode & 0o777 == 0o600
    # WAL/SHM are private even under a permissive caller umask, and existing
    # sidecars are tightened without closing an existing inode descriptor.
    with inbox._connection() as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('INSERT INTO updates(update_id,payload,kind,received_at) VALUES (?,?,?,?)', ('3', 'private message', 'json', 1))
        for suffix in ('', '-wal', '-shm'):
            path = __import__('pathlib').Path(str(inbox.path) + suffix)
            assert path.exists()
            os.chmod(path, 0o644)
        inbox._secure_files()
        for suffix in ('', '-wal', '-shm'):
            assert os.stat(str(inbox.path) + suffix).st_mode & 0o777 == 0o600
        connection.commit()
        connection.execute('PRAGMA journal_mode=DELETE')
    # Hold a rollback journal open, then kill ONLY this test-owned subprocess.
    # Its crash leaves message-bearing journal pages on disk for inspection.
    code = '''
import os, sys, time
from pathlib import Path
from plugins.platforms.telegram.update_inbox import UpdateInbox
os.umask(0)
inbox = UpdateInbox(Path(sys.argv[1]), '123')
with inbox._connection() as connection:
    connection.execute('BEGIN IMMEDIATE')
    connection.execute("UPDATE updates SET payload = 'changed private message'")
    print('transaction-ready', flush=True)
    time.sleep(30)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdout=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        assert child.stdout.readline().strip() == 'transaction-ready'
        assert os.stat(str(inbox.path) + '-journal').st_mode & 0o777 == 0o600
        child.kill()
        child.wait(timeout=5)
        assert os.stat(str(inbox.path) + '-journal').st_mode & 0o777 == 0o600
        assert os.stat(inbox.path).st_mode & 0o777 == 0o600
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
