"""Terminal JSON record for ``hermes -z --output-format json``.

agentctl's generic-process adapter (``genericParser`` / ``parseAgentJSON``,
family ``process``) stores an answer only from a structured terminal record on
stdout. A line is that record when ``type`` is ``result``; the answer is the
string ``result`` field; ``status`` ``success`` or ``failed`` sets the outcome.
Exit 0 with any other stdout is ``result_extraction_failed`` (orphaned), so a
json-format run writes this one line and nothing else to stdout.

``usage`` is included when the turn reported counters. The adapter keeps only
scalar ``usage`` values in its observation data; a usage object is still valid
input and is ignored there rather than rejected.
"""

from __future__ import annotations

import json
import math
from typing import IO, Optional

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "api_calls",
    "estimated_cost_usd",
)

_NO_RESPONSE = "hermes -z: no final response was produced; treating the run as failed."
_GENERIC_FAILURE = "hermes -z failed"


def _clean(text: str) -> str:
    """Same lone-surrogate scrub as plain ``-z`` stdout (#80366)."""
    from agent.message_sanitization import _sanitize_surrogates

    return _sanitize_surrogates(text)


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _clean(value)


def _redact(text: str) -> str:
    """Failure text can quote provider errors or tracebacks; agentctl stores
    ``error`` and ``result`` verbatim, so secrets must not survive into them."""
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True, redact_url_credentials=True)


def _failure_message(result: dict, failure: Optional[str], exit_code: int) -> str:
    if isinstance(failure, str) and failure.strip():
        return _redact(_clean(failure.strip()))
    raw_error = result.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        return _redact(_clean(raw_error.strip()))
    if exit_code == 130 or result.get("interrupted"):
        return "Interrupted"
    if exit_code == 1:
        return _NO_RESPONSE
    reason = result.get("turn_exit_reason")
    if isinstance(reason, str) and reason.strip():
        return _clean(reason.strip())
    return _GENERIC_FAILURE


def _usage_block(result: dict) -> Optional[dict]:
    usage: dict = {}
    for key in _USAGE_FIELDS:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        usage[key] = value
    return usage or None


def build_oneshot_result_record(
    *,
    response: object = None,
    result: Optional[dict] = None,
    exit_code: int,
    failure: Optional[str] = None,
) -> dict:
    """One generic-process terminal object. ``exit_code == 0`` is success."""
    payload = result if isinstance(result, dict) else {}
    text = _text(response)
    if not text:
        text = _text(payload.get("final_response"))
    success = exit_code == 0 and bool(text.strip())
    record: dict = {
        "type": "result",
        "status": "success" if success else "failed",
        "success": success,
        "is_error": not success,
    }
    if success:
        record["result"] = text
    else:
        err = _failure_message(payload, failure, exit_code)
        record["error"] = err
        # agentctl stores ``result`` as the answer. A failure that only sets
        # ``error`` leaves content empty, so the visible answer (or the error
        # itself) always rides in ``result``.
        record["result"] = _redact(text) if text.strip() else err
    for key in ("session_id", "model", "provider"):
        value = _text(payload.get(key)).strip()
        if value:
            record[key] = value
    usage = _usage_block(payload)
    if usage is not None:
        record["usage"] = usage
    return record


def format_oneshot_result_line(record: dict) -> str:
    """One JSON line. Newlines inside the answer stay escaped."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_oneshot_result_line(stream: IO[str], record: dict) -> None:
    stream.write(format_oneshot_result_line(record))
    stream.flush()
