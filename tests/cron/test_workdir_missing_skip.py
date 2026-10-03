"""A configured workdir that no longer exists skips the run and records the occurrence once."""

from __future__ import annotations

import shutil
from unittest.mock import MagicMock, patch

import cron.incidents as incidents
import cron.jobs as cron_jobs
import cron.scheduler as sched
from cron.executions import list_executions


def _point_ledger(monkeypatch, tmp_path):
    db = tmp_path / "cron" / "executions.db"
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", db)
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", db)
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(sched, "_hermes_home", tmp_path)


def test_prepare_skips_before_no_agent_script(tmp_path, monkeypatch):
    _point_ledger(monkeypatch, tmp_path)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    script = tmp_path / "ping.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    job = cron_jobs.create_job(
        prompt="", schedule="every 1h", name="script-only",
        script=str(script), no_agent=True, workdir=str(workdir))
    shutil.rmtree(workdir)

    with patch.object(sched, "_run_no_agent_job") as run_script:
        early, prompt = sched._prepare_job_prompt(job, job["id"], job["name"], None, None)
    assert run_script.call_count == 0
    assert prompt is None
    assert early[0] is False
    assert str(early[3]).startswith("workdir_missing:")

    present = tmp_path / "still-here"
    present.mkdir()
    assert sched._missing_workdir_result(
        {"workdir": str(present)}, "job", "job") is None


def test_dispatch_records_the_slot_and_notifies_once(tmp_path, monkeypatch):
    _point_ledger(monkeypatch, tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    job = cron_jobs.create_job(
        prompt="read the repo", schedule="every 1h", name="repo-scan",
        workdir=str(workdir), deliver="telegram:123")
    shutil.rmtree(workdir)
    stored = cron_jobs.get_job(job["id"])
    before_next = stored["next_run_at"]
    cron_jobs.update_job(job["id"], {"pending_slot": {"scheduled_at": before_next, "at": before_next, "by": "test"}})
    stored = cron_jobs.get_job(job["id"])

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
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as agent_cls:
        sched.run_one_job(dict(stored))
        assert agent_cls.call_count == 0
        after = cron_jobs.get_job(job["id"])
        sched.run_one_job(dict(after))
        assert agent_cls.call_count == 0

    assert len(deliveries) == 1
    assert "did not run" in deliveries[0]
    assert "does not exist" in deliveries[0]
    after = cron_jobs.get_job(job["id"])
    assert after["last_status"] == "error"
    assert str(after["last_error"]).startswith("workdir_missing:")
    assert after["next_run_at"] != before_next
    assert after.get("pending_slot") is None

    rows = list_executions(job_id=job["id"])
    assert rows and all(row["status"] == "failed" for row in rows)
    assert any(str(row.get("error") or "").startswith("workdir_missing:") for row in rows)

    found = incidents.list_incidents()
    assert len(found) == 1
    assert found[0]["id"].endswith("_workdir_missing")
    assert found[0]["failure_type"] == "workdir_missing"
    assert found[0]["state"] == "alerted"
