"""The launchd gateway runs in the app scheduling class, not launchd's throttled default.

Without ``ProcessType`` a LaunchAgent is launchd's "Standard" job (spawn type daemon): base
priority 20 with light CPU/I/O throttling, below the 31 that terminal and agent processes get.
Under load the gateway's event loop, cron ticker and fire-claim heartbeats starved for minutes
(cron fired 30-48 min late, the liveness watchdog exited 75 twice in two hours).
"""

import plistlib
import re

import hermes_cli.gateway as gateway_cli


def _generated_plist(tmp_path, monkeypatch) -> str:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
    # Carry-forward must never read the real installed plist from the test host.
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: tmp_path / "absent.plist")
    return gateway_cli.generate_launchd_plist()


def test_launchd_plist_requests_interactive_scheduling(tmp_path, monkeypatch):
    parsed = plistlib.loads(_generated_plist(tmp_path, monkeypatch).encode("utf-8"))
    assert parsed["ProcessType"] == "Interactive"
    # Interactive is level with user apps; nothing may push the gateway below (or above) them.
    assert "Nice" not in parsed
    assert "LowPriorityIO" not in parsed
    assert "LowPriorityBackgroundIO" not in parsed


def test_installed_plist_without_process_type_is_stale(tmp_path, monkeypatch):
    """An install written before this key must read as outdated, so `gateway status` flags it
    and the next `gateway restart`/update rewrites and reloads the definition."""
    generated = _generated_plist(tmp_path, monkeypatch)
    legacy = re.sub(r"\s*<key>ProcessType</key>\s*<string>Interactive</string>", "", generated)
    assert "ProcessType" not in plistlib.loads(legacy.encode("utf-8"))

    installed = tmp_path / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: installed)
    installed.write_text(legacy, encoding="utf-8")
    assert gateway_cli.launchd_plist_is_current() is False
    installed.write_text(generated, encoding="utf-8")
    assert gateway_cli.launchd_plist_is_current() is True
