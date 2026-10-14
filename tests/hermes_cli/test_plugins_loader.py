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
    import time
    manifest = SimpleNamespace(name="slow-platform", kind="platform")
    ctx = PluginContext(manifest, manager=object())
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.02)
    registered = []
    def register():
        time.sleep(0.06)
        registered.append(not ctx._load_abandoned)
        return "loaded"
    assert plugins_loader.run_with_load_deadline("slow-platform", ctx, register) == "loaded"
    assert registered == [True]
    assert "critical transport" in caplog.text


def test_platform_plugin_registers_after_threshold_through_real_loader(tmp_path, monkeypatch, caplog):
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.platform_registry import platform_registry
    plugin = tmp_path / "slow-platform"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(
        "import time\ndef register(ctx):\n    time.sleep(0.06)\n"
        "    ctx.register_platform('slow-test', 'Slow Test', lambda cfg: cfg, lambda: True)\n"
    )
    manager = PluginManager()
    manifest = PluginManifest(name="slow-platform", kind="platform", source="user", path=str(plugin))
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.02)
    try:
        manager._load_plugin(manifest)
        entry, _ = platform_registry.snapshot_registration("slow-test", scope=manager.scope_key)
        assert entry is not None and entry.check_fn()
        assert entry.adapter_factory("config") == "config"
        assert "critical transport" in caplog.text
    finally:
        manager.unload(manifest)
