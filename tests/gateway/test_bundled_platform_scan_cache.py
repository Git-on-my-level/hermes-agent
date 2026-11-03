"""All bundled-platform consumers share one serving-source scan, including concurrent profiles."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock


def test_bundled_scan_is_shared_across_config_env_registry_and_enum(monkeypatch):
    from gateway import config
    from gateway.config_env import _bundled_env_fallbacks
    from gateway.platform_env import bundled_sibling
    from gateway.platform_registry import core_ships_platform

    monkeypatch.setattr(config, '_Platform__bundled_plugin_names', None)
    monkeypatch.setattr(config, '_Platform__bundled_plugin_aliases', None)
    scan = Mock(wraps=config.Platform._scan_bundled_plugin_platforms)
    monkeypatch.setattr(config.Platform, '_scan_bundled_plugin_platforms', scan)
    bundled_sibling.cache_clear()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(pool.map(core_ships_platform, ['a2a'] * 8))
        config.Platform._missing_('unregistered-cache-probe')
        _bundled_env_fallbacks(config.GatewayConfig(), [])
        assert bundled_sibling('telegram') is not None
        assert bundled_sibling('not-a-bundled-platform') is None
        assert core_ships_platform('a2a')
        assert scan.call_count == 1
    finally:
        bundled_sibling.cache_clear()
