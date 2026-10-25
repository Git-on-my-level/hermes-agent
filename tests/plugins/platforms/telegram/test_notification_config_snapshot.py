"""Telegram rebuilds use startup/reload config snapshots, without recursive config loads."""
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('mode', ['all', 'important'])
def test_rebuild_uses_loaded_notification_snapshot_without_config_io(tmp_path, monkeypatch, mode):
    from gateway.config import PlatformConfig
    from gateway.config_loader import bridge_core_env_settings
    from plugins.platforms.telegram.adapter import _build_adapter
    from gateway import config

    monkeypatch.delenv('HERMES_TELEGRAM_NOTIFICATIONS', raising=False)
    platforms = {}
    bridge_core_env_settings({'display': {'platforms': {'telegram': {'notifications': mode}}}}, platforms)
    # Patch after loading: this is the exact factory used by reconnect, not a resolver mock.
    load = Mock(side_effect=AssertionError('config I/O during rebuild'))
    monkeypatch.setattr(config, 'load_gateway_config', load)
    pconfig = PlatformConfig(enabled=True, token='test', extra=platforms.get('telegram', {}).get('extra', {}))
    for _ in range(2):
        assert _build_adapter(pconfig)._notifications_mode == mode
    load.assert_not_called()
