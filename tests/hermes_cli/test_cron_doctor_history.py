"""Issue #37 §1/§3/§4 — ``hermes cron doctor`` history-based checks.

The doctor iterated the ACTIVE job list only, so executions whose job no longer existed were
invisible by construction, no failure rate was computable, orphaned output directories were
never reported, and pausing a job silently disarmed whatever depended on it. All four are now
doctor findings; ``--prune`` is the one destructive path (operator-invoked only).
"""

import sqlite3

import pytest

from cron import executions, jobs
from hermes_cli.cron import (
    _cron_doctor_history_findings,
    _cron_doctor_orphan_output_dirs,
    _cron_doctor_pause_propagation_findings,
    cron_doctor,
)


@pytest.fixture
def store(monkeypatch, tmp_path):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", cron_dir / "executions.db")
    return jobs


def _ledger_row(store, job_id, *, status="completed", age_days: float = 0.0):
    from datetime import timedelta
    from hermes_time import now as hermes_now

    db_path = store.JOBS_FILE.parent / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
             claimed_at TEXT NOT NULL, finished_at TEXT)""")
    stamp = hermes_now() - timedelta(days=age_days)
    conn.execute(
        "INSERT INTO executions (id, job_id, status, claimed_at, finished_at) VALUES (?,?,?,?,?)",
        (f"row-{job_id}-{status}-{age_days}", job_id, status,
         stamp.isoformat(), stamp.isoformat()))
    conn.commit()
    conn.close()


def test_ghost_running_execution_is_reported(store):
    _ledger_row(store, "ghost-job", status="running")
    _ledger_row(store, "ghost-job", status="completed")

    findings = _cron_doctor_history_findings([])

    assert len(findings) == 1
    assert "RUNNING" in findings[0] and "ghost-job" in findings[0]
    assert "stale scheduler" in findings[0]


def test_ghost_terminal_only_history_is_reported(store):
    _ledger_row(store, "gone-job", status="completed")

    findings = _cron_doctor_history_findings([])

    assert findings == [
        "1 execution record(s) for unknown job 'gone-job' — the job is gone but its history "
        "remains"]


def test_healthy_history_and_live_jobs_are_silent(store):
    job = jobs.create_job(prompt="x", schedule="every 1h")
    _ledger_row(store, job["id"], status="completed")
    _ledger_row(store, job["id"], status="failed", age_days=1)

    assert _cron_doctor_history_findings([]) == []


def test_failure_rate_reported_at_or_above_half_with_three_attempts(store):
    job = jobs.create_job(prompt="x", schedule="every 1h")
    for index in range(2):
        _ledger_row(store, job["id"], status="failed", age_days=index)
        _ledger_row(store, job["id"], status="completed", age_days=index + 0.5)

    findings = _cron_doctor_history_findings([])
    assert any("failed 2/4 runs" in line for line in findings)


def test_failure_rate_below_threshold_not_reported(store):
    job = jobs.create_job(prompt="x", schedule="every 1h")
    _ledger_row(store, job["id"], status="failed")
    for index in range(3):
        _ledger_row(store, job["id"], status="completed", age_days=index)

    assert _cron_doctor_history_findings([]) == []


def test_orphan_output_dirs_reported_excluding_known_records(store):
    job = jobs.create_job(prompt="x", schedule="every 1h")
    (store.OUTPUT_DIR / job["id"]).mkdir(parents=True)
    orphan = store.OUTPUT_DIR / "orphan-id"
    orphan.mkdir(parents=True)
    (orphan / "diag.md").write_text("trace")

    assert _cron_doctor_orphan_output_dirs([]) == [("orphan-id", 1)]


def test_pause_propagation_flags_context_from_dependent(store):
    source = jobs.create_job(prompt="source", schedule="every 1h", name="source-job")
    dependent = jobs.create_job(prompt="dep", schedule="every 1h", name="dep-job")
    records = jobs.load_jobs()
    for record in records:
        if record["id"] == source["id"]:
            record["state"] = "paused"
            record["enabled"] = False
            record["paused_at"] = __import__("hermes_time").now().isoformat()
        if record["id"] == dependent["id"]:
            record["context_from"] = [source["id"]]
    jobs.save_jobs(records)

    active = jobs.list_jobs(include_disabled=False)
    findings = _cron_doctor_pause_propagation_findings(active)

    assert len(findings) == 1
    assert "dep-job" in findings[0] and "source-job" in findings[0]
    assert "stale context" in findings[0]


def test_expect_output_watchdog_pause_flagged_as_dark(store, monkeypatch):
    from hermes_time import now as hermes_now

    watchdog = jobs.create_job(
        prompt="watch", schedule="every 5m", name="watchdog",
        script="watch.py", no_agent=True)
    records = jobs.load_jobs()
    record = records[0]
    record["expect_output"] = True
    record["state"] = "paused"
    record["enabled"] = False
    record["paused_at"] = hermes_now().isoformat()
    jobs.save_jobs(records)

    findings = _cron_doctor_pause_propagation_findings(jobs.list_jobs(include_disabled=False))

    assert len(findings) == 1
    assert "watchdog" in findings[0] and "dark" in findings[0]


def test_doctor_prune_reaps_orphans_and_reports(store, monkeypatch, capsys):
    jobs.create_job(prompt="x", schedule="every 1h")
    orphan = store.OUTPUT_DIR / "orphan-id"
    orphan.mkdir(parents=True)
    (orphan / "old.md").write_text("x")

    class Args:
        prune = True

    assert cron_doctor(Args()) == 1
    out = capsys.readouterr().out
    assert not orphan.exists()
    assert "Pruned 1" in out


def test_doctor_without_prune_only_advises(store, monkeypatch, capsys):
    jobs.create_job(prompt="x", schedule="every 1h")
    orphan = store.OUTPUT_DIR / "orphan-id"
    orphan.mkdir(parents=True)

    class Args:
        prune = False

    assert cron_doctor(Args()) == 1
    out = capsys.readouterr().out
    assert orphan.exists()
    assert "--prune" in out


def test_doctor_exit_zero_when_all_history_clean(store, monkeypatch, capsys):
    class Args:
        prune = False

    assert cron_doctor(Args()) == 0
    assert "no issues" in capsys.readouterr().out
