"""Retry utilities — jittered backoff for decorrelated retries.

Jittered delays (vs. fixed exponential) prevent thundering-herd retry spikes
when many sessions hit the same rate-limited provider concurrently.
"""

import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Monotonic counter for jitter-seed uniqueness within a process; locked
# because concurrent gateway sessions retry simultaneously.
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan's GLM endpoints return 429s in two shapes: code 1305 ("service
# may be temporarily overloaded", observed on glm-5.2) and code 1302 ("Rate limit
# reached for requests", a concurrency cap, observed on glm-5.3-flash). Both persist
# for minutes — far longer than the ~10s a default 3-attempt/2s-base backoff covers —
# so after ``_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS`` normal retries the wait widens
# progressively. 2026-09-20: sustained 1302 storms on glm-5.3-flash still outlasted
# the original 30/60/90/120s window (~8 attempts, ~6 min) — turns died and a manual
# re-prompt minutes later completed — so the schedule now extends with 180s/300s and
# holds 300s for any further attempts (~16 min worst case before giving up).
# The short count is shared by ``adaptive_rate_limit_backoff`` and
# ``zai_coding_overload_retry_ceiling`` so the two cannot silently desync.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0, 180.0, 300.0)
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = 3


def parse_retry_after_seconds(value_or_headers: Any) -> Optional[float]:
    """Parse a ``Retry-After`` value (numeric / HTTP-date) or a headers mapping (both casings tried) into
    seconds, clamped at 0.0; None when absent / unparseable."""
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except Exception:
            return None
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # older stdlib returns None instead of raising
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# Free-text "reset" grammars providers put in error bodies, tried in order. One table so the
# conversation loop's error context and the credential pool's cooldown agree on the same wait.
_QUOTA_RESET_DELAY_RE = re.compile(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE)
# "Resets in 4hr 5min" (weekly usage limits), "resets in 2 hours 5 minutes", "resets in 30s".
_RESETS_IN_RE = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?", re.IGNORECASE,
)
_RETRY_AFTER_SECONDS_RE = re.compile(r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", re.IGNORECASE)
# The plan usage-limit body field as it appears once stringified: ``'resets_in_seconds': 30995``.
_RESETS_IN_SECONDS_FIELD_RE = re.compile(r"resets_in_seconds\W{1,4}(\d+(?:\.\d+)?)", re.IGNORECASE)


def _quota_reset_seconds(m: "re.Match[str]") -> float:
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _resets_in_seconds(m: "re.Match[str]") -> Optional[float]:
    if not any(m.groups()):  # "resets in" with no unit-bearing number: not this grammar
        return None
    return float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60 + float(m.group(3) or 0)


# An explicit "retry after N s" wins over "resets in ..." (the credential pool's precedence):
# a body carrying both describes a short throttle inside a long quota window, and the
# shorter explicit wait is the one the provider actually asks for.
RETRY_DELAY_PATTERNS = (
    (_QUOTA_RESET_DELAY_RE, _quota_reset_seconds),
    (_RETRY_AFTER_SECONDS_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_SECONDS_FIELD_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_RE, _resets_in_seconds),
)


def format_reset_window(seconds: float) -> str:
    """``~9h`` / ``~45 min`` for chat copy naming when a quota window reopens (ceilinged)."""
    seconds = int(seconds)
    return f"~{-(-seconds // 3600)}h" if seconds >= 3600 else f"~{-(-seconds // 60)} min"


def reset_delay_from_message(message: str) -> Optional[float]:
    """Seconds-until-reset parsed from free-text provider error messages, or None."""
    if not message:
        return None
    for pattern, to_seconds in RETRY_DELAY_PATTERNS:
        m = pattern.search(message)
        if m and (seconds := to_seconds(m)) is not None:
            return seconds
    return None


def jittered_backoff(attempt: int, *, base_delay: float = 5.0, max_delay: float = 120.0, jitter_ratio: float = 0.5) -> float:
    """min(base * 2^(attempt-1), max_delay) + uniform jitter in
    [0, jitter_ratio * delay]. ``attempt`` is 1-based."""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    delay = max_delay if (exponent >= 63 or base_delay <= 0) else min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter so coarse clocks still decorrelate.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    return delay + random.Random(seed).uniform(0, jitter_ratio * delay)


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [error, getattr(error, "message", None), getattr(error, "body", None), getattr(error, "response", None)]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_plan_429(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True for any 429 from the Z.AI Coding Plan endpoint (any GLM model): the
    plan's concurrency/overload 429 family (codes 1302 and 1305) persists for
    minutes, so all of it needs the long-backoff schedule, not fast-fail.

    Match the ``/coding/paas/v4`` path so both first-class Coding Plan hosts
    (``api.z.ai`` and ``open.bigmodel.cn``) get the schedule, without widening
    to general ``/paas/v4`` 429s.
    """
    text = _error_text(error)
    return (
        getattr(error, "status_code", None) == 429
        and "/coding/paas/v4" in (base_url or "").lower()
        and ("glm" in (model or "").lower())
        and ("1302" in text or "1305" in text or "rate limit" in text or "temporarily overloaded" in text)
    )


# Peak-load drops on the Coding Plan endpoint often never return a 429. Studio
# logs them as ``error=Connection error.`` (OpenAI ``APIConnectionError``,
# sometimes with a DNS cause) and the turn dies at ``policy=default`` attempt
# 1/3–2/3. Same schedule as the 429 family — do not invent a second tuple.
# ponytail: a genuinely offline Mac on a GLM Coding Plan turn waits this same
# ~16 min window, because peak-load resets and DNS failures share that wrapper.
# Upgrade path: split on cause-chain DNS markers once a peak-load sample lacks them.
_ZAI_CODING_TRANSPORT_TYPES = frozenset({
    "APIConnectionError", "APITimeoutError",
    "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError", "WriteError", "WriteTimeout",
    "RemoteProtocolError", "PoolTimeout", "NetworkError",
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
    "TimeoutError", "ConnectTimeoutError", "ReadTimeoutError",
})
_ZAI_CODING_GATEWAY_TIMEOUT_STATUSES = frozenset({502, 503, 504, 524})
_ZAI_CODING_SSL_CERT_MARKERS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "unable to get local issuer certificate",
    "self-signed certificate",
    "self signed certificate",
    "certificate has expired",
)
_ZAI_CODING_TRANSPORT_TEXT_MARKERS = (
    "connection error",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "getaddrinfo failed",
    "network is unreachable",
    "network unreachable",
)


def _zai_coding_glm(base_url: str | None, model: str | None) -> bool:
    return "/coding/paas/v4" in (base_url or "").lower() and "glm" in (model or "").lower()


def _exception_chain(error: Any):
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def is_zai_coding_plan_transport_failure(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True for Coding Plan GLM connection/timeout drops that are not HTTP 429s.

    Peak load presents as ``Connection error.`` / read timeouts / 502–524, classified
    as timeout, and the 429-only predicate leaves them on the 3-attempt default.
    4xx (including 429) and TLS cert failures stay fail-fast.
    """
    if error is None or not _zai_coding_glm(base_url, model):
        return False
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:
        return False
    chain = list(_exception_chain(error))
    text = " ".join(str(part) for part in chain if part is not None).lower()
    if any(marker in text for marker in _ZAI_CODING_SSL_CERT_MARKERS):
        return False
    if isinstance(status, int) and status in _ZAI_CODING_GATEWAY_TIMEOUT_STATUSES:
        return True
    names = {type(part).__name__ for part in chain}
    if names & _ZAI_CODING_TRANSPORT_TYPES:
        return True
    if not isinstance(status, int) and any(marker in text for marker in _ZAI_CODING_TRANSPORT_TEXT_MARKERS):
        return True
    return False


def is_zai_coding_sustained_outage(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True when a Coding Plan GLM call should use the long-backoff ceiling.

    Covers the 429 family and the connection/timeout family. ``is_zai_coding_plan_429``
    stays the narrow 429 predicate (tests and any external reader).
    """
    return is_zai_coding_plan_429(
        base_url=base_url, model=model, error=error,
    ) or is_zai_coding_plan_transport_failure(
        base_url=base_url, model=model, error=error,
    )


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True only for the narrow Z.AI Coding Plan overload shape (429 + code
    1305 / "temporarily overloaded"), so ordinary quota 429s still fail fast."""
    text = _error_text(error)
    return (
        getattr(error, "status_code", None) == 429
        and "api.z.ai/api/coding/paas/v4" in (base_url or "").lower()
        and "glm-5.2" in (model or "").lower()
        and ("1305" in text or "temporarily overloaded" in text)
    )


def adaptive_rate_limit_backoff(
    attempt: int, *, base_url: str | None, model: str | None, error: Any, default_wait: float,
    short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
) -> tuple[float, str | None]:
    """``(wait_seconds, reason_label)``: ``default_wait`` for most providers.

    Z.AI Coding Plan GLM failures that persist for minutes — 429s (1302/1305) and
    connection/timeout drops — keep ``short_attempts`` short retries, then the shared
    long tuple. ``attempt`` is 1-based. Policy labels stay distinct so logs can tell
    a 429 from a connection drop.
    """
    if is_zai_coding_plan_429(base_url=base_url, model=model, error=error):
        family = "zai_coding_overload"
    elif is_zai_coding_plan_transport_failure(base_url=base_url, model=model, error=error):
        family = "zai_coding_transport"
    else:
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, f"{family}_short"
    idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
    base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
    return jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2), f"{family}_long"


def zai_coding_overload_retry_ceiling(short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS) -> int:
    """Retry-loop ceiling for the full Z.AI overload schedule: one past the last long entry,
    because the loop gives up when ``retry_count >= ceiling`` BEFORE computing the attempt's
    backoff (the default ``api_max_retries`` of 3 equals ``short_attempts``)."""
    return short_attempts + len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) + 1
