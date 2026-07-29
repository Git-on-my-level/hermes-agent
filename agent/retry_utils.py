"""Retry utilities — jittered backoff for decorrelated retries.

Replaces fixed exponential backoff with jittered delays to prevent
thundering-herd retry spikes when multiple sessions hit the same
rate-limited provider concurrently.
"""

import random
import threading
import time
from typing import Any

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI / GLM transient 429s (rate-limit, overload, resource_exhausted) often
# clear within a few minutes. Short retries tend to hammer the same window;
# after a few normal retries, progressively widen the wait. Keep the overall
# schedule interactive-friendly (~3–10 minutes) so a temporary throttle does
# not force the user to re-send, while hard quota/billing still fails closed
# via the classifier exclusion list below.
_ZAI_GLM_TRANSIENT_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)

# Number of initial short retries before the adaptive long-backoff tier kicks
# in. Shared by ``adaptive_rate_limit_backoff`` (which walks the long table
# starting at attempt ``short_attempts + 1``) and
# ``zai_glm_transient_retry_ceiling`` (which sizes the retry loop so every
# long-tier entry is reachable). Keeping it a single module constant prevents
# the two from silently desyncing if the short-retry count is ever tuned.
_ZAI_GLM_TRANSIENT_SHORT_ATTEMPTS = 3

# Legacy aliases — older call sites / docs referred to the narrower
# "Z.AI Coding Plan GLM-5.2 overload" path. Behavior is now the broader
# Z.AI/GLM transient throttle policy above.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = _ZAI_GLM_TRANSIENT_LONG_BACKOFF
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = _ZAI_GLM_TRANSIENT_SHORT_ATTEMPTS

# Hard account/quota exhaustion signals on Z.AI. These must NOT get the
# multi-minute patience schedule — fail closed / rotate / surface billing.
_ZAI_GLM_HARD_QUOTA_PATTERNS = (
    "insufficient balance",
    "insufficient_quota",
    "insufficient credits",
    "insufficient_credits",
    "balance_depleted",
    "out of credits",
    "out of funds",
    "payment required",
    "billing hard limit",
    "no usable credits",
    "1113",  # Z.AI insufficient-balance error code
)


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range.  0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
        getattr(error, "response", None),
    ]
    return " ".join(str(part) for part in parts if part is not None).lower()


def _is_zai_endpoint(base_url: str | None) -> bool:
    """True for Z.AI / Zhipu OpenAI-compatible endpoints (coding or paas)."""
    base = (base_url or "").lower()
    return (
        "api.z.ai" in base
        or "zhipuai.cn" in base
        or "bigmodel.cn" in base
        or "/paas/v4" in base
    )


def _is_glm_model(model: str | None) -> bool:
    """True for GLM model family names used on Z.AI (incl. vision variants)."""
    model_name = (model or "").lower()
    # Strip provider prefixes like ``zai/glm-5v-turbo`` or ``z-ai/glm-5.2``.
    if "/" in model_name:
        model_name = model_name.rsplit("/", 1)[-1]
    return model_name.startswith("glm") or "glm-" in model_name


def _is_zai_hard_quota_error(text: str) -> bool:
    return any(pattern in text for pattern in _ZAI_GLM_HARD_QUOTA_PATTERNS)


def is_zai_glm_transient_throttle_error(
    *,
    base_url: str | None,
    model: str | None,
    error: Any,
) -> bool:
    """Return True for transient Z.AI/GLM 429 / throttle errors.

    Covers coding-plan and paas endpoints, GLM chat + vision models
    (``glm-5.2``, ``glm-5v-turbo``, …), and bodies that say rate-limit /
    overloaded / resource_exhausted / opaque 429. Excludes hard
    billing/balance exhaustion so those still fail closed through the
    existing classifier + credential-pool path.

    Inspired by OMP's ``parseRateLimitReason`` /
    ``isUsageLimitOutcome`` split: transient rate-limit/capacity stays on
    same-credential backoff; true quota/billing does not.
    """
    status = getattr(error, "status_code", None)
    if status != 429:
        return False
    if not _is_zai_endpoint(base_url) or not _is_glm_model(model):
        return False
    text = _error_text(error)
    if _is_zai_hard_quota_error(text):
        return False
    return True


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """Backward-compatible alias for :func:`is_zai_glm_transient_throttle_error`.

    Historically this matched only Coding Plan + ``glm-5.2`` + code 1305 /
    "temporarily overloaded". The patient-backoff path now covers the full
    Z.AI/GLM transient-throttle class; keep the old name so existing imports
    and status-policy labels continue to work.
    """
    return is_zai_glm_transient_throttle_error(
        base_url=base_url, model=model, error=error
    )


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    base_url: str | None,
    model: str | None,
    error: Any,
    default_wait: float,
    short_attempts: int = _ZAI_GLM_TRANSIENT_SHORT_ATTEMPTS,
) -> tuple[float, str | None]:
    """Provider-aware rate-limit backoff.

    For most providers this returns ``default_wait`` unchanged. For Z.AI/GLM
    transient 429s, keep the first ``short_attempts`` retries on the normal
    short exponential schedule, then switch to progressively longer waits
    (30s → 60s → 90s → 120s, capped) plus light jitter — roughly a
    multi-minute patience window without requiring the user to re-send.

    ``attempt`` is 1-based, matching the retry loop's logged attempt number.
    Returns ``(wait_seconds, reason_label)`` where ``reason_label`` is suitable
    for status/log decoration when a provider-specific policy fired.
    """
    if not is_zai_glm_transient_throttle_error(
        base_url=base_url, model=model, error=error
    ):
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, "zai_coding_overload_short"

    idx = min(attempt - short_attempts - 1, len(_ZAI_GLM_TRANSIENT_LONG_BACKOFF) - 1)
    base_delay = _ZAI_GLM_TRANSIENT_LONG_BACKOFF[idx]
    # A smaller jitter ratio keeps long waits readable while still avoiding
    # synchronized retry storms across concurrent Hermes sessions.
    return (
        jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2),
        "zai_coding_overload_long",
    )


def zai_glm_transient_retry_ceiling(
    short_attempts: int = _ZAI_GLM_TRANSIENT_SHORT_ATTEMPTS,
) -> int:
    """Retry-loop ceiling needed for the full Z.AI/GLM transient backoff schedule.

    The adaptive policy runs ``short_attempts`` short retries, then walks the
    long-backoff table one entry per subsequent attempt. The retry loop gives
    up as soon as ``retry_count >= ceiling`` — and that check runs *before* the
    attempt's backoff is computed — so the ceiling must sit one past the final
    long-backoff entry for every long tier to actually execute.

    With the default ``api_max_retries`` (3) equal to ``short_attempts`` (3),
    the loop always gave up before reaching the long tier, leaving the whole
    long-backoff schedule as dead code. Callers extend the ceiling to this
    value for Z.AI/GLM transient 429s so the 30/60/90/120s waits run.
    """
    return short_attempts + len(_ZAI_GLM_TRANSIENT_LONG_BACKOFF) + 1


def zai_coding_overload_retry_ceiling(
    short_attempts: int = _ZAI_GLM_TRANSIENT_SHORT_ATTEMPTS,
) -> int:
    """Backward-compatible alias for :func:`zai_glm_transient_retry_ceiling`."""
    return zai_glm_transient_retry_ceiling(short_attempts)
