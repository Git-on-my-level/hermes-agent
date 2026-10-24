"""Platform rebuild I/O must not starve the canonical local session transport (SCA-1078)."""
import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import connect

from gateway.config import Platform
from tests.gateway.test_platform_reconnect import _make_runner, StubAdapter
from tests.gateway.test_owner_loop_writes_class import _owner


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.DISCORD])
async def test_platform_rebuild_stall_keeps_native_session_create_responsive(tmp_path, monkeypatch, platform):
    from gateway.run_api import start_gateway_api, stop_gateway_api
    from gateway.runtime_bootstrap import TicketStore
    from gateway.session_contract import CANONICAL_GATEWAY_PROTOCOL

    async def handle(event):
        return 'ok'

    store, authority, owner = _owner(tmp_path, monkeypatch, handle)
    authority.profile_id = str(authority.db.db_path.parent.resolve())
    from gateway.session_controls import AuthorityConnection
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    api_runner = authority.runner
    api_runner.session_authority = authority
    api_runner.session_runtime_descriptor = {'state': 'ready'}
    api_runner.session_ticket_store = TicketStore(authority.instance_id, [authority.profile_id])
    runner = _make_runner()
    runner._flag_reconnect_needs_attention = Mock()
    runner._wire_adapter_handlers = Mock()
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)
    runner._install_reconnected_adapter = AsyncMock()
    runner._run_in_executor_with_context = asyncio.to_thread
    pconfig = runner.config.platforms[Platform.TELEGRAM]
    runner._failed_platforms[platform] = dict(config=pconfig, attempts=0, next_retry=0)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    started = []

    def stalled_factory(platform, config):
        started.append(time.monotonic())
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)  # synchronous filesystem-like stall, also finite on the unfixed branch
        return StubAdapter(platform=platform)

    if platform is Platform.TELEGRAM:
        runner._create_adapter = stalled_factory
    else:
        from gateway import platform_registry as registry_module
        from gateway.platform_registry import PlatformEntry, PlatformRegistry
        registry = PlatformRegistry()

        def probe():
            stalled_factory(platform, pconfig)
            return True

        def factory(config):
            # A general plugin's constructor retains its loop affinity after blocking preparation.
            assert asyncio.get_running_loop() is loop
            return StubAdapter(platform=platform)

        entry = PlatformEntry(name=platform.value, label='Fake platform', check_fn=probe,
                              adapter_factory=factory, source='builtin')
        registry.register(entry, scope=registry.current_scope_key())
        monkeypatch.setattr(registry_module, 'platform_registry', registry)
    api = await start_gateway_api(api_runner)
    reconnect = None
    try:
        # Safe mode exercises real admission/commit without provider/workspace discovery;
        # the timing contract here isolates platform stalls from unrelated cold-start I/O.
        # Warm the session.create policy/import path, outside the measured platform stall.
        reply = await owner.dispatch({'id': 'warm', 'method': 'session.create', 'params': {
            'request_id': 'warm', 'source': 'cli', 'cwd': str(tmp_path), 'model': 'gpt-4.1-mini', 'toolsets': [], 'safe_mode': True}})
        assert 'result' in reply, reply
        url = api.api_origin.replace('http:', 'ws:') + '/api/ws'
        ticket = api_runner.session_ticket_store.mint(
            profile_id=authority.profile_id, subject='human', purpose='interactive')
        async with connect(url, subprotocols=[CANONICAL_GATEWAY_PROTOCOL, 'hermes-gateway-ticket.' + ticket]) as ws:
            ready = json.loads(await asyncio.wait_for(ws.recv(), 30))
            assert ready['params']['type'] == 'gateway.ready', ready
            if platform is Platform.DISCORD:
                registry.register(entry, scope=registry.current_scope_key())  # cold plugin discovery during ready may register the real adapter
            reconnect = asyncio.create_task(runner._reconnect_failed_platform(platform, time.monotonic()))
            try:
                await asyncio.wait_for(entered.wait(), 10)
            except TimeoutError:
                pytest.fail(f"rebuild never reached the injected stall: {runner._failed_platforms!r}; "
                            f"task={reconnect!r}")
            await ws.send(json.dumps({'id': 'create', 'method': 'session.create', 'params': {
                'request_id': 'during-rebuild', 'source': 'tui', 'cwd': str(tmp_path),
                'model': 'gpt-4.1-mini', 'toolsets': [], 'safe_mode': True}}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply.get('id') == 'create':
                    break
            elapsed = time.monotonic() - started[0]
            assert 'result' in reply, reply
            assert elapsed < 1, f'native session.create took {elapsed:.3f}s during a 2s rebuild stall'
    finally:
        release.set()
        if reconnect is not None:
            await reconnect
        await stop_gateway_api(api)
        await owner.close()
        store.close_all_db_handles()
