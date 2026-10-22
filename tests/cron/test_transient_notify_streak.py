"""Transient failures stay detected, and page only after N consecutive ones. Default N is 1."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import cron.incidents as incidents
import cron.jobs as cron_jobs
import cron.scheduler as sched
from cron.scheduler_failure_copy import is_transient_cron_failure, transient_notice_withheld

_IDLE = "TimeoutError: tool idle for 30s (limit 10s)"


def _point_ledger(monkeypatch, tmp_path):
    db = tmp_path / "cron" / "executions.db"
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", db)
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", db)
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(sched, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("cron:\n  preflight: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _run_failing(job, error):
    deliveries = []

    def fake_deliver(jb, content, adapters=None, loop=None, **kwargs):
        deliveries.append(content)
        return None

    fake_db = MagicMock()
    with patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": "openrouter",
                   "api_mode": "chat_completions",
               }), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as agent_cls:
        agent = MagicMock()
        agent.run_conversation.side_effect = RuntimeError(error)
        agent_cls.return_value = agent
        sched.run_one_job(dict(job))
    return deliveries


def test_classifier_covers_the_self_healing_set():
    assert is_transient_cron_failure({}, _IDLE) is True
    assert is_transient_cron_failure({}, "Script timed out after 10s: /tmp/x.sh") is True
    assert is_transient_cron_failure({"_model_unreachable": True}, "connection reset") is True
    assert is_transient_cron_failure({}, "produced empty response") is True
    assert is_transient_cron_failure({}, "cron_incomplete_no_output") is True
    assert is_transient_cron_failure({}, "config.yaml is not valid") is False
    # Default threshold never withholds, whatever the streak says.
    assert transient_notice_withheld({"transient_failure_streak": 5}, _IDLE) is False
    assert transient_notice_withheld(
        {"transient_notify_after": 3, "transient_failure_streak": 1}, _IDLE) is True
    assert transient_notice_withheld(
        {"transient_notify_after": 3, "transient_failure_streak": 2}, _IDLE) is False


def test_default_threshold_still_delivers_the_first_transient(tmp_path, monkeypatch):
    _point_ledger(monkeypatch, tmp_path)
    job = cron_jobs.create_job(
        prompt="ping", schedule="every 1h", name="default-threshold", deliver="telegram:123")
    deliveries = _run_failing(cron_jobs.get_job(job["id"]), _IDLE)
    assert len(deliveries) == 1
    row = incidents.list_incidents()[0]
    assert row["state"] == "alerted"
    assert cron_jobs.get_job(job["id"])["transient_failure_streak"] == 1


def test_streak_withholds_until_threshold_then_a_real_failure_resets_it(tmp_path, monkeypatch):
    _point_ledger(monkeypatch, tmp_path)
    job = cron_jobs.create_job(
        prompt="ping", schedule="every 1h", name="streak", deliver="telegram:123")
    cron_jobs.update_job(job["id"], {"transient_notify_after": 2})

    first = _run_failing(cron_jobs.get_job(job["id"]), _IDLE)
    assert first == []
    stored = cron_jobs.get_job(job["id"])
    assert stored["transient_failure_streak"] == 1
    assert stored["last_status"] == "error"
    incident = incidents.list_incidents()[0]
    assert incident["state"] == "detected"

    second = _run_failing(stored, _IDLE)
    assert len(second) == 1
    assert incidents.list_incidents()[0]["state"] == "alerted"
    assert cron_jobs.get_job(job["id"])["transient_failure_streak"] == 2

    _run_failing(cron_jobs.get_job(job["id"]), "config.yaml is not valid")
    assert cron_jobs.get_job(job["id"])["transient_failure_streak"] == 0


def test_config_threshold_applies_when_the_job_has_no_override(tmp_path, monkeypatch):
    _point_ledger(monkeypatch, tmp_path)

    real = cron_jobs._cron_config_number

    def _config_number(key, default, cast):
        if key == "transient_notify_after":
            return 2
        return real(key, default, cast)

    # The helper imports the reader at call time; patch the defining module.
    monkeypatch.setattr(cron_jobs, "_cron_config_number", _config_number)
    job = cron_jobs.create_job(
        prompt="ping", schedule="every 1h", name="from-config", deliver="telegram:123")
    assert _run_failing(cron_jobs.get_job(job["id"]), _IDLE) == []
    assert incidents.list_incidents()[0]["state"] == "detected"
    assert cron_jobs.get_job(job["id"])["transient_failure_streak"] == 1
