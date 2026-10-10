"""Opt-in latency-pressure compaction trigger.

Incident shape: glm-5.3-flash's catalog window is 1.31M tokens, so the ratio threshold
(≈1.11M) never fires while the zai coding endpoint already crawls at ~470K tokens — a
watchdog session spent its whole life issuing multi-minute API calls with the token gauge
nowhere near the compaction line. The opt-in guard compacts on observed slowness instead.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.context_compressor import ContextCompressor


def _compressor(**kw) -> ContextCompressor:
    return ContextCompressor(
        model="glm-5.3-flash", threshold_percent=0.85, protect_first_n=1, protect_last_n=1,
        quiet_mode=True, **kw
    )


def _feed(compressor: ContextCompressor, samples) -> None:
    """samples: list of (latency_seconds, prompt_tokens) already in the window."""
    now = time.monotonic()
    compressor.api_latency_samples = [
        (now - (len(samples) - i), float(d), int(t)) for i, (d, t) in enumerate(samples)
    ]


# ---------------------------------------------------------------------------
# Sampling + the guard itself
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    c = _compressor()
    assert c.latency_pressure_threshold_seconds == 0.0
    _feed(c, [(300.0, 400_000), (300.0, 400_000), (300.0, 400_000)])
    assert c.latency_pressure() is False


def test_fires_when_median_over_threshold():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(130.0, 400_000), (300.0, 450_000), (240.0, 500_000)])
    assert c.latency_pressure() is True


def test_does_not_fire_when_median_under_threshold():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(30.0, 400_000), (300.0, 450_000), (60.0, 500_000)])
    assert c.latency_pressure() is False


def test_needs_minimum_sample_count():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(300.0, 400_000), (300.0, 450_000)])
    assert c.latency_pressure() is False


def test_min_context_token_floor_filters_samples():
    c = _compressor(
        latency_pressure_threshold_seconds=120, latency_pressure_min_context_tokens=100_000
    )
    # Two huge slow samples + one tiny fast one: without the floor the median is 130 (fires);
    # with it, only 2 samples survive and the count gate blocks.
    _feed(c, [(30.0, 5_000), (300.0, 400_000), (130.0, 450_000)])
    assert c.latency_pressure() is False
    _feed(c, [(30.0, 5_000), (300.0, 400_000), (240.0, 450_000), (300.0, 500_000)])
    assert c.latency_pressure() is True


def test_window_excludes_stale_samples():
    c = _compressor(latency_pressure_threshold_seconds=120)
    now = time.monotonic()
    # Two fresh slow samples + one slow sample 20 minutes old: the stale one is dropped.
    c.api_latency_samples = [
        (now - 1200.0, 300.0, 400_000), (now - 30.0, 300.0, 450_000), (now - 10.0, 240.0, 500_000),
    ]
    assert c.latency_pressure() is False


def test_record_via_update_from_response_uses_passed_latency():
    c = _compressor(latency_pressure_threshold_seconds=120)
    for latency, prompt in ((300.0, 400_000), (260.0, 420_000), (280.0, 440_000)):
        c.update_from_response(
            {"prompt_tokens": prompt, "completion_tokens": 10}, api_latency_seconds=latency
        )
    assert c.latency_pressure() is True


def test_model_switch_clears_samples():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(300.0, 400_000), (300.0, 450_000), (240.0, 500_000)])
    c.update_model(model="other-model", context_length=128_000, provider="x")
    assert c.api_latency_samples == []


# ---------------------------------------------------------------------------
# Preflight wiring (stubbed compressor so the turn-start hook is exercised end to end)
# ---------------------------------------------------------------------------


def _preflight_agent(compressor):
    return SimpleNamespace(
        compression_enabled=True, context_compressor=compressor, session_id="s1",
        model="glm-5.3-flash", _clear_context_overflow_warn=MagicMock(),
        _turn_received_provider_response=False, _turn_preflight_display_snapshot=None,
        _emit_status=MagicMock(), max_compression_attempts=1,
        _request_pressure_anchored=True,
    )


@pytest.mark.asyncio
async def test_preflight_compacts_on_latency_pressure_below_token_threshold():
    from agent.turn_context_compaction import run_turn_start_compaction

    compressor = _compressor(latency_pressure_threshold_seconds=120)
    # 300K tokens vs a 1.11M threshold: the token trigger must NOT fire...
    compressor.threshold_tokens = 1_114_000
    compressor.context_length = 1_310_720
    compressor.should_compress = MagicMock(return_value=False)
    compressor.should_defer_preflight_to_real_usage = lambda _tokens: False
    compressor.get_active_compression_failure_cooldown = lambda: None
    compressor.snapshot_preflight_display_tokens = None
    compressor.maybe_seed_preflight_display_tokens = None
    agent = _preflight_agent(compressor)
    agent._compress_context = MagicMock(
        side_effect=lambda messages, system, **kw: (messages[:1], system)
    )
    compressor.latency_pressure = lambda: True
    # Enough rows to pass the cheap preflight estimate gate (protect 1 + 1 + 1).
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(8)]
    out = run_turn_start_compaction(
        agent, messages=msgs, system_message=None, active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=7, user_message="msg 7",
        effective_task_id="t",
    )
    # ...but the latency-pressure trigger ran the compaction pass.
    assert agent._compress_context.called
    assert out.compressed is True


@pytest.mark.asyncio
async def test_preflight_ignores_latency_pressure_when_disabled():
    from agent.turn_context_compaction import run_turn_start_compaction

    compressor = _compressor()  # threshold 0 = off
    compressor.threshold_tokens = 1_114_000
    compressor.context_length = 1_310_720
    compressor.should_compress = MagicMock(return_value=False)
    compressor.should_defer_preflight_to_real_usage = lambda _tokens: False
    compressor.get_active_compression_failure_cooldown = lambda: None
    compressor.snapshot_preflight_display_tokens = None
    compressor.maybe_seed_preflight_display_tokens = None
    agent = _preflight_agent(compressor)
    agent._compress_context = MagicMock()
    # Enough rows to REACH the trigger branch (the review's finding: a 1-message transcript
    # bails at the cheap estimate gate and never consults latency_pressure).
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(8)]
    out = run_turn_start_compaction(
        agent, messages=msgs, system_message=None, active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=7, user_message="msg 7",
        effective_task_id="t",
    )
    assert not agent._compress_context.called
    assert out.compressed is False


# ---------------------------------------------------------------------------
# Review fixes: sampling hygiene + epoch reset
# ---------------------------------------------------------------------------


def test_no_usage_update_records_no_sample_and_no_stale_latency():
    c = _compressor(latency_pressure_threshold_seconds=120)
    c.last_api_latency_seconds = 300.0  # from a PREVIOUS real call
    c.update_from_response({})  # codex_runtime / turn_usage no-usage path
    assert c.api_latency_samples == []
    # Even with a latency passed, a zero prompt count is never sampled.
    c.update_from_response({}, api_latency_seconds=300.0)
    assert c.api_latency_samples == []


def test_explicit_latency_required_for_sampling():
    c = _compressor(latency_pressure_threshold_seconds=120)
    # Real usage but no latency passed (e.g. codex_runtime's real-usage path): no sample.
    c.update_from_response({"prompt_tokens": 400_000, "completion_tokens": 10})
    assert c.api_latency_samples == []
    # With latency: sampled.
    c.update_from_response(
        {"prompt_tokens": 400_000, "completion_tokens": 10}, api_latency_seconds=300.0
    )
    assert len(c.api_latency_samples) == 1
    assert c.api_latency_samples[0][1] == 300.0 and c.api_latency_samples[0][2] == 400_000


def test_even_count_median_uses_true_middle_average():
    c = _compressor(latency_pressure_threshold_seconds=120)
    # 4 samples: sorted [60, 240, 240, 300] -> median (240+240)/2 = 240 >= 120 fires.
    _feed(c, [(60.0, 400_000), (300.0, 450_000), (240.0, 500_000), (240.0, 500_000)])
    assert c.latency_pressure() is True
    # sorted [10, 20, 100, 110] -> median 60 < 120 does not.
    _feed(c, [(10.0, 400_000), (100.0, 450_000), (20.0, 500_000), (110.0, 500_000)])
    assert c.latency_pressure() is False


def test_committed_compaction_resets_the_latency_epoch():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(300.0, 400_000), (300.0, 450_000), (240.0, 500_000)])
    assert c.latency_pressure() is True
    # A committed compaction (compression_count bump site) clears the evidence:
    c.api_latency_samples = []
    assert c.latency_pressure() is False


def test_session_reset_clears_samples():
    c = _compressor(latency_pressure_threshold_seconds=120)
    _feed(c, [(300.0, 400_000), (300.0, 450_000), (240.0, 500_000)])
    c._reset_session_compaction_state()
    assert c.api_latency_samples == []


def test_window_reaped_on_write():
    c = _compressor(latency_pressure_threshold_seconds=120)
    now = time.monotonic()
    c.api_latency_samples = [(now - 1200.0, 300.0, 400_000)]
    c.last_api_latency_seconds = 300.0
    c.update_from_response(
        {"prompt_tokens": 450_000, "completion_tokens": 10}, api_latency_seconds=300.0
    )
    # The stale sample was reaped at write time; only the fresh one remains.
    assert len(c.api_latency_samples) == 1
    assert c.api_latency_samples[0][2] == 450_000


def test_config_bool_rejected_for_threshold():
    from agent.agent_init import _parse_config_float

    # YAML `true` must fall back to default, not become 1.0 seconds.
    assert _parse_config_float(True, 0.0) == 0.0
    assert _parse_config_float("120", 0.0) == 120.0
    assert _parse_config_float(120, 0.0) == 120.0
    assert _parse_config_float("garbage", 0.0) == 0.0


def test_compressor_ctor_rejects_bool_threshold():
    c = ContextCompressor(
        model="m", threshold_percent=0.85, quiet_mode=True,
        latency_pressure_threshold_seconds=True,  # would coerce to 1.0s if unguarded
    )
    assert c.latency_pressure_threshold_seconds == 0.0
