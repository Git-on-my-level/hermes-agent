"""Invariants that must pass before a tip is released to fork/prod.

A fleet that auto-follows fork/prod will install whatever this tree contains.
These checks fail closed if a sync drops the pieces that keep that follow from
bricking: the idle tick, a failed fetch must not look current, and the
coordinator opt-out that refuses to recycle the owning gateway.
"""
from hermes_cli.update_converge import ConvergeSettings, decide_converge

PIN = "7580268954af1c8d7298de4279d661ee872960c7"
OTHER = "3b4dca6834ac8689e3365d803deb44f91e1cfc64"


def _settings(**overrides) -> ConvergeSettings:
    base = dict(
        enabled=True,
        pin="",
        interval=1800,
        busy_sla=21600,
        skip_gateway_restart=False,
        remote="fork",
        branch="prod",
    )
    base.update(overrides)
    return ConvergeSettings(**base)


def test_failed_fetch_is_not_already_current():
    decision = decide_converge(
        settings=_settings(),
        checkout_sha=PIN,
        live_sha=PIN,
        dirty=False,
        busy=False,
        pin_age_s=0,
        target_sha=PIN,
        channel_fetch_ok=False,
    )
    assert decision.action == "skip"
    assert decision.reason == "channel_fetch_failed"


def test_good_fetch_at_tip_is_already_current():
    decision = decide_converge(
        settings=_settings(),
        checkout_sha=PIN,
        live_sha=PIN,
        dirty=False,
        busy=False,
        pin_age_s=0,
        target_sha=PIN,
        channel_fetch_ok=True,
    )
    assert decision.reason == "already_current"


def test_behind_host_still_updates():
    decision = decide_converge(
        settings=_settings(),
        checkout_sha=OTHER,
        live_sha=OTHER,
        dirty=False,
        busy=False,
        pin_age_s=0,
        target_sha=PIN,
        channel_fetch_ok=True,
    )
    assert decision.action == "update"
    assert decision.reason == "channel_tip"


def test_skip_gateway_restart_does_not_recycle_a_current_checkout():
    # Owning coordinator: checkout matches, process must not be restarted.
    decision = decide_converge(
        settings=_settings(skip_gateway_restart=True),
        checkout_sha=PIN,
        live_sha="",
        dirty=False,
        busy=False,
        pin_age_s=0,
        target_sha=PIN,
        channel_fetch_ok=True,
    )
    assert decision.action == "skip"
    assert decision.reason == "already_current"


def test_stale_runtime_is_a_restart_unless_opted_out():
    decision = decide_converge(
        settings=_settings(skip_gateway_restart=False),
        checkout_sha=PIN,
        live_sha=OTHER,
        dirty=False,
        busy=False,
        pin_age_s=10**9,
        target_sha=PIN,
        channel_fetch_ok=True,
    )
    assert decision.action == "restart"
    assert decision.reason == "stale_runtime"


def test_opt_out_is_explicit_not_the_default():
    assert _settings().skip_gateway_restart is False
    assert _settings().enabled is True
