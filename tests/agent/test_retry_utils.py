"""Tests for agent.retry_utils jittered backoff."""

import threading

import agent.retry_utils as retry_utils
from types import SimpleNamespace

from agent.retry_utils import adaptive_rate_limit_backoff, is_zai_coding_overload_error, jittered_backoff


def test_backoff_is_exponential():
    """Base delay should double each attempt (before jitter)."""
    for attempt in (1, 2, 3, 4):
        delays = [jittered_backoff(attempt, base_delay=5.0, max_delay=120.0, jitter_ratio=0.0) for _ in range(100)]
        expected = min(5.0 * (2 ** (attempt - 1)), 120.0)
        mean = sum(delays) / len(delays)
        assert abs(mean - expected) < 0.01, f"attempt {attempt}: expected {expected}, got {mean}"


def test_backoff_respects_max_delay():
    """Even with high attempt numbers, delay should not exceed max_delay."""
    for attempt in (10, 20, 100):
        delay = jittered_backoff(attempt, base_delay=5.0, max_delay=60.0, jitter_ratio=0.0)
        assert delay <= 60.0, f"attempt {attempt}: delay {delay} exceeds max 60s"




def test_backoff_attempt_1_is_base():
    """First attempt delay should equal base_delay (with no jitter)."""
    delay = jittered_backoff(1, base_delay=3.0, max_delay=120.0, jitter_ratio=0.0)
    assert delay == 3.0








def test_backoff_thread_safety():
    """Concurrent calls should generally produce different delays."""
    results = []
    barrier = threading.Barrier(8)

    def _call_backoff():
        barrier.wait()
        results.append(jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5))

    threads = [threading.Thread(target=_call_backoff) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    unique = len(set(results))
    assert unique >= 6, f"Expected mostly unique delays, got {unique}/8 unique"


def test_backoff_uses_locked_tick_for_seed(monkeypatch):
    """Seed derivation should use per-call tick captured under lock."""
    import time

    monkeypatch.setattr(retry_utils, "_jitter_counter", 0)

    recorded_seeds = []

    class _RecordingRandom:
        def __init__(self, seed):
            recorded_seeds.append(seed)

        def uniform(self, a, b):
            return 0.0

    monkeypatch.setattr(retry_utils.random, "Random", _RecordingRandom)

    fixed_time_ns = 123456789

    def _time_ns_wait_for_two_ticks():
        deadline = time.time() + 2.0
        while retry_utils._jitter_counter < 2 and time.time() < deadline:
            time.sleep(0.001)
        return fixed_time_ns

    monkeypatch.setattr(retry_utils.time, "time_ns", _time_ns_wait_for_two_ticks)

    barrier = threading.Barrier(2)

    def _call():
        barrier.wait()
        jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(recorded_seeds) == 2
    assert len(set(recorded_seeds)) == 2, f"Expected unique seeds, got {recorded_seeds}"


def _zai_overload_error():
    return SimpleNamespace(
        status_code=429,
        body={
            "error": {
                "code": "1305",
                "message": "The service may be temporarily overloaded, please try again later",
            }
        },
    )










def test_zai_overload_retry_ceiling_exceeds_short_attempts():
    """Invariant: the ceiling must sit above the short-retry threshold, or the
    long-backoff tier is unreachable and the whole schedule is dead code
    (the original bug: default api_max_retries == short_attempts == 3)."""
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    short_attempts = 3
    ceiling = zai_coding_overload_retry_ceiling(short_attempts)
    assert ceiling > short_attempts
    # Invariant (not a formula mirror): the loop's give-up check
    # (retry_count >= ceiling) runs *before* the attempt's backoff, so the
    # ceiling must leave headroom for every long-backoff entry to execute —
    # i.e. the largest attempt the loop still computes backoff for
    # (ceiling - 1) must reach the final long-tier index.
    last_attempt_with_backoff = ceiling - 1
    assert last_attempt_with_backoff - short_attempts >= len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_zai_overload_ceiling_makes_long_tier_reachable(monkeypatch):
    """End-to-end over the attempt range the retry loop actually walks: with the
    extended ceiling, at least one attempt reaches the long-backoff tier and the
    full 30/60/90/120s schedule is exercised."""
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    err = _zai_overload_error()
    ceiling = zai_coding_overload_retry_ceiling()

    long_waits = []
    # The loop computes backoff for attempts 1..ceiling-1 (it gives up at ceiling).
    for attempt in range(1, ceiling):
        _wait, policy = adaptive_rate_limit_backoff(
            attempt,
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            error=err,
            default_wait=1.0,
        )
        if policy == "zai_coding_overload_long":
            long_waits.append(_wait)

    assert long_waits, "long-backoff tier never reached within the retry ceiling"
    # Ratchet: every long-tier entry must be exercised, so widening the schedule
    # updates this list automatically instead of silently truncating it.
    assert long_waits == list(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def _zai_concurrency_error():
    """The glm-5.3-flash production shape: 429 code 1302 concurrency cap."""
    return SimpleNamespace(
        status_code=429,
        body={"error": {"code": "1302", "message": "Rate limit reached for requests"}},
    )



def test_zai_concurrency_429_reaches_long_tier(monkeypatch):
    """Regression (2026-09-18 fleet outage): glm-5.3-flash 429 code 1302 must get the
    long-backoff schedule within the retry ceiling — on the old glm-5.2/1305-only
    predicate it fell through to policy=default (3 tries, ~10s) and the turn died."""
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    ceiling = zai_coding_overload_retry_ceiling()
    long_waits = []
    for attempt in range(1, ceiling):
        _wait, policy = adaptive_rate_limit_backoff(
            attempt,
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.3-flash",
            error=_zai_concurrency_error(),
            default_wait=1.0,
        )
        if policy == "zai_coding_overload_long":
            long_waits.append(_wait)

    assert long_waits == list(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_zai_concurrency_429_window_covers_sustained_storms():
    """Regression (2026-09-20): the 30/60/90/120s window (~6 min) still died during
    sustained 1302 storms that cleared after ~10 min; a manual re-prompt completed.
    The widened schedule must cover a >=10-minute throttle window without human help."""
    from agent.retry_utils import (
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
        _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
    )

    # Every tier must strictly increase so waits keep widening (no plateau below the cap).
    tiers = list(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)
    assert tiers == sorted(tiers) and len(set(tiers)) == len(tiers)
    # Worst-case survival window: short retries (~14s) + the long-tier waits the
    # loop actually sleeps. The ceiling's +1 is the give-up check
    # (retry_count >= ceiling) and never waits — neighboring tests already
    # prove long waits equal the tuple over range(1, ceiling).
    short_window = sum(min(2.0 * 2 ** n, 60.0) for n in range(_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS))
    total = short_window + sum(tiers)
    assert total >= 600, f"survival window {total}s < 10min; storms will still kill turns"


def test_zai_china_coding_plan_429_reaches_long_tier(monkeypatch):
    """China Coding Plan lives on open.bigmodel.cn/api/coding/paas/v4; a
    glm-5.3-flash 1302 there must get the same long-backoff schedule as Global,
    not the default short window that a host-literal ``api.z.ai`` check would
    leave it on."""
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    ceiling = zai_coding_overload_retry_ceiling()
    long_waits = []
    for attempt in range(1, ceiling):
        _wait, policy = adaptive_rate_limit_backoff(
            attempt,
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            model="glm-5.3-flash",
            error=_zai_concurrency_error(),
            default_wait=1.0,
        )
        if policy == "zai_coding_overload_long":
            long_waits.append(_wait)

    assert long_waits == list(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_non_coding_plan_429_still_fails_fast():
    """The wide predicate must not swallow ordinary 429s: different endpoint,
    non-GLM model, or plain quota bodies keep the default short backoff."""
    from agent.retry_utils import is_zai_coding_plan_429

    err = _zai_concurrency_error()
    assert not is_zai_coding_plan_429(
        base_url="https://api.openai.com/v1", model="gpt-5", error=err)
    assert not is_zai_coding_plan_429(
        base_url="https://api.z.ai/api/coding/paas/v4", model="gpt-5", error=err)
    assert not is_zai_coding_plan_429(
        base_url="https://api.z.ai/api/coding/paas/v4", model="glm-5.3-flash",
        error=SimpleNamespace(status_code=500, body=err.body))
    # General (non-coding) Z.AI hosts share the /paas/v4 suffix; matching the
    # Coding Plan path must not widen to them.
    assert not is_zai_coding_plan_429(
        base_url="https://api.z.ai/api/paas/v4", model="glm-5.3-flash", error=err)
    assert not is_zai_coding_plan_429(
        base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-5.3-flash", error=err)


# ---------------------------------------------------------------------------
# parse_retry_after_seconds — shared Retry-After parser
# ---------------------------------------------------------------------------


class TestParseRetryAfterSeconds:
    def test_numeric_string(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds("120") == 120.0
        assert parse_retry_after_seconds(" 4.5 ") == 4.5

    def test_numeric_value(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds(45) == 45.0
        assert parse_retry_after_seconds(3.25) == 3.25


    def test_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        from agent.retry_utils import parse_retry_after_seconds

        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        seconds = parse_retry_after_seconds(format_datetime(future, usegmt=True))
        assert seconds is not None and 80 <= seconds <= 91

        past = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert parse_retry_after_seconds(format_datetime(past, usegmt=True)) == 0.0



    def test_headers_get_raises(self):
        from agent.retry_utils import parse_retry_after_seconds

        class Explosive:
            def get(self, _key):
                raise RuntimeError("boom")

        assert parse_retry_after_seconds(Explosive()) is None
