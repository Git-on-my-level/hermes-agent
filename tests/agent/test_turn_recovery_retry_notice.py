"""A provider retry backoff names itself on the live status line.

The buffered retry status replays only if every retry fails, so during the
backoff itself the user used to see an anonymous spinner — and right after a
tool that just finished (a connector sign-in landing, say) it read as the
agent going silent. The wait notice is transient (rewritten by the next
frame, cleared on recovery) and rides the frame long provider waits already
use, so it adds none of the transcript chatter the buffer exists to avoid."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_recovery import compute_error_backoff


def _backoff_agent():
    agent = MagicMock()
    from agent.status_output import StatusOutputMixin
    for name in (
        "_emit_diagnostic_wait",
        "_buffer_diagnostic_status",
        "_emit_diagnostic_status",
    ):
        setattr(agent, name, getattr(StatusOutputMixin, name).__get__(agent))
    agent._client_log_context.return_value = ""
    return agent


def _gateway_timeout(status_code, retry_after):
    err = RuntimeError(f"{status_code} gateway timeout")
    err.status_code = status_code
    err.response = SimpleNamespace(headers={"Retry-After": str(retry_after)})
    return err


@pytest.mark.real_retry_backoff
def test_retry_backoff_names_the_wait_on_the_live_status_line():
    agent = _backoff_agent()

    wait = compute_error_backoff(
        agent, RuntimeError("502"), retry_count=1, max_retries=3,
        is_rate_limited=False, is_zai_coding_overload=False,
        base_url="https://example.test/v1", model="test/model",
    )

    assert wait > 0
    # Still buffered for the exhausted-retries replay …
    agent._buffer_status.assert_called_once()
    # … and named live while the backoff runs.
    agent._emit_wait_notice.assert_called_once()
    text = agent._emit_wait_notice.call_args.args[0]
    assert text.startswith("⏳ waiting on provider")
    assert "attempt 1/3" in text


@pytest.mark.real_retry_backoff
def test_long_retry_after_on_zai_adaptive_path_emits_immediately():
    """A 5xx Retry-After on the Z.AI Coding adaptive path skips the `_long`
    policy label. The wait must still surface immediately — buffering it
    leaves the user silent for the whole cooldown."""
    agent = _backoff_agent()

    wait = compute_error_backoff(
        agent, _gateway_timeout(524, 120), retry_count=1, max_retries=8,
        is_rate_limited=False, is_zai_coding_overload=True,
        base_url="https://api.z.ai/api/coding/paas/v4", model="glm-5.3-flash",
    )

    assert wait == 120.0
    agent._emit_status.assert_called_once()
    agent._buffer_status.assert_not_called()
    assert "Waiting 120.0s" in agent._emit_status.call_args.args[0]


@pytest.mark.real_retry_backoff
def test_short_retry_after_on_zai_adaptive_path_stays_buffered():
    """Short provider cooldowns keep the buffered status line even after
    502/503/504/524 join the adaptive family."""
    agent = _backoff_agent()

    wait = compute_error_backoff(
        agent, _gateway_timeout(503, 30), retry_count=1, max_retries=8,
        is_rate_limited=False, is_zai_coding_overload=True,
        base_url="https://api.z.ai/api/coding/paas/v4", model="glm-5.3-flash",
    )

    assert wait == 30.0
    agent._buffer_status.assert_called_once()
    agent._emit_status.assert_not_called()
    assert "Waiting 30.0s" in agent._buffer_status.call_args.args[0]
