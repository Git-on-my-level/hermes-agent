"""Policy tests for idle/after-turn SHA converge (no fleetctl)."""
from __future__ import annotations

from hermes_cli.update_converge import (
    ConvergeSettings,
    decide_converge,
    load_converge_settings,
    normalize_pin,
    pin_age_seconds,
    prefixes_match,
    read_pin_file,
)

PIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PIN2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHORT = "aaaaaaaa"


def _s(
    enabled: bool = True,
    pin: str = PIN,
    interval: int = 1800,
    busy_sla: float = 21600.0,
    skip_gateway_restart: bool = False,
) -> ConvergeSettings:
    return ConvergeSettings(
        enabled=enabled, pin=pin, interval=interval,
        busy_sla=busy_sla, skip_gateway_restart=skip_gateway_restart,
    )


def test_normalize_pin_rejects_garbage():
    assert normalize_pin("not-a-sha") == ""
    assert normalize_pin("deadbeef") == "deadbeef"
    assert normalize_pin("SHA:DEADBEEF\n") == "deadbeef"


def test_prefixes_match_short_and_long():
    assert prefixes_match(SHORT, PIN)
    assert not prefixes_match(PIN, PIN2)


def test_skip_when_disabled_or_unpinned():
    d = decide_converge(settings=_s(enabled=False), checkout_sha="1", live_sha="", dirty=False, busy=False, pin_age_s=0)
    assert d.action == "skip" and d.reason == "disabled"


def test_empty_pin_follows_channel_tip():
    d = decide_converge(settings=_s(pin=""), checkout_sha="1", live_sha="", dirty=False, busy=False, pin_age_s=0)
    assert d.action == "update" and d.reason == "channel_tip"
    d = decide_converge(settings=_s(pin=""), checkout_sha="1", live_sha="", dirty=True, busy=False, pin_age_s=0)
    assert d.action == "skip" and d.reason == "dirty_tree"
    d = decide_converge(settings=_s(pin=""), checkout_sha="1", live_sha="", dirty=False, busy=True, pin_age_s=10)
    assert d.action == "skip" and d.reason == "busy"
    d = decide_converge(settings=_s(pin=""), checkout_sha="1", live_sha="", dirty=False, busy=True, pin_age_s=21600)
    assert d.action == "update" and d.reason == "channel_tip"


def test_empty_pin_skips_when_already_on_tip():
    d = decide_converge(
        settings=_s(pin=""), checkout_sha=PIN, live_sha=PIN, dirty=False, busy=False, pin_age_s=0,
        target_sha=PIN,
    )
    assert d.action == "skip" and d.reason == "already_current"


def test_empty_pin_restarts_stale_runtime():
    d = decide_converge(
        settings=_s(pin=""), checkout_sha=PIN, live_sha=PIN2, dirty=False, busy=False, pin_age_s=0,
        target_sha=PIN,
    )
    assert d.action == "restart" and d.reason == "stale_runtime"


def test_already_current_skips():
    d = decide_converge(
        settings=_s(), checkout_sha=PIN, live_sha=PIN, dirty=False, busy=False, pin_age_s=0,
    )
    assert d.action == "skip" and d.reason == "already_current"


def test_skip_restart_treats_checkout_match_as_current():
    d = decide_converge(
        settings=_s(skip_gateway_restart=True),
        checkout_sha=PIN, live_sha="cccccccccccccccccccccccccccccccccccccccc",
        dirty=False, busy=False, pin_age_s=0,
    )
    assert d.action == "skip" and d.reason == "already_current"


def test_stale_runtime_restarts():
    d = decide_converge(
        settings=_s(), checkout_sha=PIN, live_sha=PIN2, dirty=False, busy=False, pin_age_s=0,
    )
    assert d.action == "restart" and d.reason == "stale_runtime"


def test_behind_updates():
    d = decide_converge(
        settings=_s(), checkout_sha=PIN2, live_sha=PIN2, dirty=False, busy=False, pin_age_s=0,
    )
    assert d.action == "update" and d.reason == "checkout_behind_pin"


def test_dirty_blocks_update_not_restart():
    d = decide_converge(
        settings=_s(), checkout_sha=PIN2, live_sha=PIN2, dirty=True, busy=False, pin_age_s=0,
    )
    assert d.action == "skip" and d.reason == "dirty_tree"
    d = decide_converge(
        settings=_s(), checkout_sha=PIN, live_sha=PIN2, dirty=True, busy=False, pin_age_s=0,
    )
    assert d.action == "restart"


def test_busy_skips_until_sla():
    d = decide_converge(
        settings=_s(), checkout_sha=PIN2, live_sha=PIN2, dirty=False, busy=True, pin_age_s=10,
    )
    assert d.action == "skip" and d.reason == "busy"
    d = decide_converge(
        settings=_s(), checkout_sha=PIN2, live_sha=PIN2, dirty=False, busy=True, pin_age_s=21600,
    )
    assert d.action == "update"


def test_read_pin_file_and_config(tmp_path, monkeypatch):
    pin_path = tmp_path / "updates.pin"
    pin_path.write_text(PIN + "\n", encoding="utf-8")
    assert read_pin_file(pin_path) == PIN
    s = load_converge_settings({
        "updates": {
            "converge": True,
            "pin": PIN2,
            "converge_interval": 120,
            "converge_busy_sla": 60,
            "skip_gateway_restart": True,
        }
    })
    assert s.enabled and s.pin == PIN2 and s.interval == 120 and s.busy_sla == 60 and s.skip_gateway_restart


def test_pin_age_resets_on_new_pin(tmp_path):
    first = pin_age_seconds(PIN, home=tmp_path, now=1000.0)
    assert first == 0.0
    aged = pin_age_seconds(PIN, home=tmp_path, now=1300.0)
    assert aged == 300.0
    reset = pin_age_seconds(PIN2, home=tmp_path, now=1400.0)
    assert reset == 0.0


def test_linux_timer_unit_text():
    from hermes_cli.update_converge import generate_converge_systemd_timer

    text = generate_converge_systemd_timer(_s(interval=120))
    assert "OnUnitActiveSec=120" in text
    assert "Unit=hermes-converge.service" in text
    assert "WantedBy=timers.target" in text


def test_linux_service_runs_converge_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.update_converge import generate_converge_systemd_service

    text = generate_converge_systemd_service(_s())
    assert "update --converge -y" in text
    assert "Type=oneshot" in text
    assert f"HERMES_HOME={tmp_path}" in text
