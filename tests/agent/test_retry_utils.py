"""Tests for agent.retry_utils jittered backoff."""

import threading

import agent.retry_utils as retry_utils
from types import SimpleNamespace

from agent.retry_utils import adaptive_rate_limit_backoff, jittered_backoff


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
    from agent.retry_utils import zai_coding_overload_retry_ceiling

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
    from agent.retry_utils import _ZAI_CODING_OVERLOAD_LONG_BACKOFF
    assert long_waits == list(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_zai_rate_limit_family_preserves_adaptive_and_quota_boundaries():
    endpoint = "https://api.z.ai/api/coding/paas/v4"
    for model in ("glm-5.2", "glm-5.3-flash", "GLM-5.4"):
        for payload in (
            {"code": "1302"},
            {"code": "1305"},
            {"message": "Rate limit reached for requests"},
            {"message": "Temporarily overloaded"},
            {"message": "Rate limit reached for requests. Retry after 10 s"},
        ):
            error = SimpleNamespace(status_code=429, body={"error": payload})
            assert retry_utils.is_zai_coding_overload_error(base_url=endpoint, model=model, error=error)
            for attempt in range(1, retry_utils.zai_coding_overload_retry_ceiling() + 2):
                wait, policy = adaptive_rate_limit_backoff(
                    attempt, base_url=endpoint, model=model, error=error, default_wait=2.0,
                )
                if attempt <= 3:
                    assert (wait, policy) == (2.0, "zai_coding_overload_short")
                else:
                    assert policy == "zai_coding_overload_long"

    excluded = [
        ("https://api.openai.com/v1", "glm-5.3-flash", 429, "1302"),
        (endpoint, "other-model", 429, "1302"),
        (endpoint, "glm-5.3-flash", 500, "1302"),
    ]
    for marker in ("Resets in 4hr 5min.", "Reset in 4 hours.", "quotaResetDelay: 300s", "resets_in_seconds: 300"):
        excluded.append((endpoint, "glm-5.3-flash", 429, f"1302 Rate limit reached for requests. {marker}"))
    for base_url, model, status_code, message in excluded:
        error = SimpleNamespace(status_code=status_code, body={"error": {"message": message}})
        assert not retry_utils.is_zai_coding_overload_error(base_url=base_url, model=model, error=error)
        assert adaptive_rate_limit_backoff(
            4, base_url=base_url, model=model, error=error, default_wait=2.0,
        ) == (2.0, None)

    # Classifier-recognized rate-limit wordings without a numeric body code.
    for message in ("Too many requests", "Request throttled", "rate_limit exceeded"):
        error = SimpleNamespace(status_code=429, body={"error": {"message": message}})
        assert retry_utils.is_zai_coding_overload_error(base_url=endpoint, model="glm-5.3-flash", error=error)
        assert adaptive_rate_limit_backoff(
            4, base_url=endpoint, model="glm-5.3-flash", error=error, default_wait=2.0,
        )[1] == "zai_coding_overload_long"


def test_zai_rate_limit_window_covers_ten_minutes():
    assert (2 + 4 + 8) + 1.2 * sum(retry_utils._ZAI_CODING_OVERLOAD_LONG_BACKOFF) >= 600


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
