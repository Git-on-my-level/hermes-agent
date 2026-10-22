import threading

import pytest

from hermes_cli import plugins_loader


def test_nested_plugin_load_runs_inline_on_deadline_worker(monkeypatch):
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.3)
    abandoned = []

    class Context:
        def __init__(self, name):
            self.name = name

        def _abandon_load(self):
            abandoned.append(self.name)

    observed_threads = []
    release = threading.Event()

    def outer_load():
        observed_threads.append(threading.current_thread())
        plugins_loader.run_with_load_deadline(
            "done", Context("done"), lambda: observed_threads.append(threading.current_thread()),
        )
        plugins_loader.run_with_load_deadline("hung", Context("hung"), release.wait)

    with pytest.raises(plugins_loader.PluginLoadTimeout):
        plugins_loader.run_with_load_deadline("outer", Context("outer"), outer_load)
    release.set()

    assert len(observed_threads) == 2
    assert observed_threads[0] is observed_threads[1]
    # The outer timeout abandons only contexts still loading, not a nested load that already finished.
    assert abandoned == ["outer", "hung"]


def test_registration_after_the_load_deadline_is_ignored(monkeypatch):
    """A plugin that registers after its import deadline must not land in a registry."""
    from hermes_cli.plugins import PluginContext

    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.15)
    manifest = type("Manifest", (), {"name": "slow-plugin"})()
    ctx = PluginContext(manifest, manager=object())
    release = threading.Event()
    finished = threading.Event()
    late = {}

    def slow_import():
        release.wait(5)
        late["result"] = ctx.register_tool(object())
        finished.set()

    with pytest.raises(plugins_loader.PluginLoadTimeout):
        plugins_loader.run_with_load_deadline("slow-plugin", ctx, slow_import)
    assert ctx._load_abandoned is True
    release.set()
    assert finished.wait(2)
    assert late["result"] is None


def test_slow_platform_registration_is_retained_and_alerted(monkeypatch, caplog):
    from types import SimpleNamespace
    from hermes_cli.plugins import PluginContext
    manifest = SimpleNamespace(name="slow-platform", kind="platform")
    ctx = PluginContext(manifest, manager=object())
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.02)
    registered = []
    release = threading.Event()
    def register():
        assert release.wait(10)
        registered.append(not ctx._load_abandoned)
        return "loaded"
    with pytest.raises(plugins_loader.PlatformLoadPending) as caught:
        plugins_loader.run_with_load_deadline("slow-platform", ctx, register)
    release.set()
    caught.value.worker.join(2)
    assert caught.value.outcome == ['loaded']
    assert registered == [True]
    assert "in background" in caplog.text


@pytest.mark.parametrize('phase', ['import', 'register'])
def test_pending_platform_does_not_block_other_platform_and_registers_late(tmp_path, monkeypatch, caplog, phase):
    import sys
    import time
    from types import SimpleNamespace
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.platform_registry import platform_registry
    release, entered = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, '_platform_load_signals', SimpleNamespace(release=release, entered=entered))
    slow = tmp_path / 'slow-platform'
    fast = tmp_path / 'fast-platform'
    slow.mkdir(); fast.mkdir()
    pause = "    entered.set()\n    release.wait(10)\n" if phase == 'register' else ''
    prefix = "from _platform_load_signals import entered, release\n"
    if phase == 'import':
        prefix += "entered.set()\nrelease.wait(10)\n"
    (slow / '__init__.py').write_text(prefix + "def register(ctx):\n" + pause +
        "    ctx.register_platform('slow-test', 'Slow', lambda cfg: cfg, lambda: True)\n", encoding='utf-8')
    (fast / '__init__.py').write_text("def register(ctx):\n    ctx.register_platform('fast-test', 'Fast', lambda cfg: cfg, lambda: True)\n", encoding='utf-8')
    manager = PluginManager()
    slow_manifest = PluginManifest(name='slow-platform', kind='platform', source='user', path=str(slow))
    fast_manifest = PluginManifest(name='fast-platform', kind='platform', source='user', path=str(fast))
    monkeypatch.setattr(plugins_loader, '_resolve_plugin_load_timeout', lambda: 0.02)
    try:
        token = plugins_loader._IN_PLUGIN_LOAD.set([]) if phase == 'import' else None
        try:
            manager._load_plugin(slow_manifest)
        finally:
            if token is not None:
                plugins_loader._IN_PLUGIN_LOAD.reset(token)
        assert entered.wait(2)
        assert platform_registry.snapshot_registration('slow-test', scope=manager.scope_key)[0] is None
        manager._load_plugin(fast_manifest)
        for _ in range(500):
            if platform_registry.snapshot_registration('fast-test', scope=manager.scope_key)[0]:
                break
            time.sleep(.01)
        assert platform_registry.snapshot_registration('fast-test', scope=manager.scope_key)[0]
        assert not release.is_set()
        release.set()
        for _ in range(500):
            loaded = manager._plugins.get('slow-platform')
            if loaded and not loaded.deferred:
                break
            time.sleep(.01)
        entry, _ = platform_registry.snapshot_registration('slow-test', scope=manager.scope_key)
        assert entry and entry.adapter_factory('config') == 'config'
        assert loaded.enabled and not loaded.error
        assert 'in background' in caplog.text
    finally:
        release.set()
        manager.unload()


def test_unloading_pending_platform_cancels_late_registration(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.platform_registry import platform_registry
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setitem(sys.modules, '_platform_cancel_signals', SimpleNamespace(release=release, finished=finished))
    plugin = tmp_path/'cancel-platform'
    plugin.mkdir()
    (plugin/'__init__.py').write_text("from _platform_cancel_signals import release, finished\ndef register(ctx):\n    release.wait(10)\n    ctx.register_platform('cancel-test','Cancel',lambda cfg:cfg,lambda:True)\n    finished.set()\n", encoding='utf-8')
    manager = PluginManager()
    manifest = PluginManifest(name='cancel-platform',kind='platform',source='user',path=str(plugin))
    monkeypatch.setattr(plugins_loader,'_resolve_plugin_load_timeout',lambda:.02)
    try:
        manager._load_plugin(manifest)
        assert not finished.is_set()
        assert manager.unload(manifest)
        release.set()
        assert finished.wait(5)
        assert platform_registry.snapshot_registration('cancel-test',scope=manager.scope_key)[0] is None
        assert 'cancel-platform' not in manager._plugins
    finally:
        release.set()
        manager.unload()


def test_metadata_lookup_completes_without_blocking_transport_or_discovery(tmp_path, monkeypatch):
    import contextvars
    import sys
    from types import SimpleNamespace
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.platform_registry import platform_registry

    release, entered, returned, handed_off = (threading.Event() for _ in range(4))
    monkeypatch.setitem(sys.modules, '_metadata_load_signals', SimpleNamespace(release=release, entered=entered))
    path = tmp_path / 'metadata-platform'
    path.mkdir()
    (path / '__init__.py').write_text(
        "from _metadata_load_signals import release, entered\n"
        "def register(ctx):\n"
        "    entered.set()\n"
        "    assert release.wait(10)\n"
        "    ctx.register_platform('metadata', 'Metadata', lambda cfg: cfg, lambda: True,\n"
        "                          required_env=['METADATA_TEST_TOKEN'])\n", encoding='utf-8')
    manager = PluginManager()
    manifest = PluginManifest(name='metadata-platform', kind='platform', source='user', path=str(path))
    manager._register_deferred_platform(manifest)
    wait_metadata = manager._wait_for_platform_metadata

    def metadata_wait(key):
        handed_off.set()
        return wait_metadata(key)

    monkeypatch.setattr(manager, '_wait_for_platform_metadata', metadata_wait)
    result = {}

    def lookup():
        try:
            result['entry'] = platform_registry.get('metadata')
        except BaseException as exc:
            result['error'] = exc
        finally:
            returned.set()

    caller = threading.Thread(target=contextvars.copy_context().run, args=(lookup,))
    caller.start()
    try:
        assert entered.wait(5)
        assert handed_off.wait(5)
        assert not returned.is_set()
        # The worker is held by an event, so a factory must return without
        # waiting for this metadata caller. No wall-clock race is asserted.
        assert platform_registry.create_adapter('metadata', object()) is None
        assert not release.is_set()
        assert manager._discovery_lock.acquire(blocking=False)
        manager._discovery_lock.release()
        release.set()
        caller.join(5)
        assert returned.is_set() and 'error' not in result
        assert result['entry'].required_env == ['METADATA_TEST_TOKEN']
    finally:
        release.set()
        caller.join(5)
        manager.unload()
