"""Open incidents resolve when their job is removed or finishes successfully.

A failed final run leaves the incident open. The retention sweep that later
drops the record resolves it the same way removal does. A late alert does not
walk a resolved-with-reason row back to alerted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

import cron.incidents as incidents
from cron.jobs import create_job, get_due_jobs, get_job, load_jobs, mark_job_run, remove_job, save_jobs
from cron.scheduler import _RunDelivery, _finish_completed_run
from hermes_cli.cron import cron_doctor, cron_incidents


@pytest.fixture()
def cron_home(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    db = tmp_path / "cron" / "executions.db"
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", db)
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", db)
    return tmp_path


def test_remove_resolves_open_incidents_and_leaves_closed_ones(cron_home):
    job = create_job(prompt="going away", schedule="every 1h", name="removable")
    open_id, _ = incidents.upsert_incident(job["id"], "provider blew up")
    closed_id, _ = incidents.upsert_incident(job["id"], "operator already handled this")
    assert incidents.ack_incident(closed_id) is True

    assert remove_job(job["id"]) is True
    assert get_job(job["id"]) is None

    opened = incidents.get_incident(open_id)
    assert opened["state"] == "resolved"
    assert opened["resolution_reason"] == "job removed"
    closed = incidents.get_incident(closed_id)
    assert closed["state"] == "closed"
    assert not closed.get("resolution_reason")


def test_failed_final_run_leaves_the_incident_open_and_success_resolves(cron_home):
    failed = create_job(prompt="finite", schedule="every 1h", name="budget", repeat=1)
    failed_inc, _ = incidents.upsert_incident(failed["id"], "still failing")
    assert mark_job_run(failed["id"], success=False, error="still failing") is True
    assert get_job(failed["id"])["state"] == "completed"
    open_row = incidents.get_incident(failed_inc)
    assert open_row["state"] == "detected"
    assert not (open_row.get("resolution_reason") or "").strip()

    budget = create_job(prompt="finite ok", schedule="every 1h", name="budget-ok", repeat=1)
    budget_inc, _ = incidents.upsert_incident(budget["id"], "earlier failure")
    assert mark_job_run(budget["id"], success=True) is True
    resolved = incidents.get_incident(budget_inc)
    assert resolved["state"] == "resolved"
    assert resolved["resolution_reason"] == "repeat budget exhausted"

    once = create_job(prompt="once", schedule="in 2h", name="oneshot")
    once_inc, _ = incidents.upsert_incident(once["id"], "oneshot failed earlier")
    assert mark_job_run(once["id"], success=True) is True
    assert get_job(once["id"])["state"] == "completed"
    assert incidents.get_incident(once_inc)["resolution_reason"] == "job completed"
    assert incidents.get_incident(once_inc)["state"] == "resolved"


def _delivered(job: dict, *, success: bool, error: str, incident_id: str) -> _RunDelivery:
    return _RunDelivery(
        job=job,
        success=success,
        error=None if success else error,
        should_deliver=True,
        failure_incident_id=incident_id,
    )


def test_failed_oneshot_through_finish_completed_run_stays_alerted(cron_home):
    job = create_job(prompt="once", schedule="in 2h", name="fail-shot", deliver="telegram:1")
    incident_id, _ = incidents.upsert_incident(job["id"], "provider blew up")
    assert _finish_completed_run(
        _delivered(get_job(job["id"]), success=False, error="provider blew up", incident_id=incident_id),
        None,
        "exec-unused",
    ) is True
    row = incidents.get_incident(incident_id)
    assert get_job(job["id"])["state"] == "completed"
    assert row["state"] == "alerted"
    assert not (row.get("resolution_reason") or "").strip()


def test_successful_final_run_through_finish_completed_run_resolves(cron_home):
    job = create_job(prompt="once", schedule="in 2h", name="ok-shot", deliver="telegram:1")
    incident_id, _ = incidents.upsert_incident(job["id"], "earlier failure")
    assert _finish_completed_run(
        _delivered(get_job(job["id"]), success=True, error="", incident_id=incident_id),
        None,
        "exec-unused",
    ) is True
    row = incidents.get_incident(incident_id)
    assert row["state"] == "resolved"
    assert row["resolution_reason"] == "job completed"


def test_retention_sweep_resolves_open_incidents_for_pruned_jobs(cron_home):
    job = create_job(prompt="old", schedule="in 2h", name="old-shot")
    incident_id, _ = incidents.upsert_incident(job["id"], "final failure")
    assert mark_job_run(job["id"], success=False, error="final failure") is True
    assert incidents.get_incident(incident_id)["state"] == "detected"
    stamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = load_jobs()
    for row in rows:
        if row["id"] == job["id"]:
            row["last_run_at"] = stamp
    save_jobs(rows)

    get_due_jobs()

    assert get_job(job["id"]) is None
    resolved = incidents.get_incident(incident_id)
    assert resolved["state"] == "resolved"
    assert resolved["resolution_reason"] == "job removed"


def test_resolved_with_reason_is_not_flipped_by_a_late_alert(cron_home):
    job = create_job(prompt="held", schedule="every 1h", name="held")
    incident_id, _ = incidents.upsert_incident(job["id"], "boom")
    assert incidents.resolve_open_incidents(job["id"], "job removed") == 1
    assert incidents.set_incident_state(incident_id, "alerted") is False
    assert incidents.set_incident_state(incident_id, "detected") is False
    row = incidents.get_incident(incident_id)
    assert row["state"] == "resolved"
    assert row["resolution_reason"] == "job removed"

    # A new occurrence of the same error is the reopen. That path may then alert.
    reopened, _ = incidents.upsert_incident(job["id"], "boom")
    assert reopened == incident_id
    assert incidents.get_incident(incident_id)["state"] == "detected"
    assert incidents.set_incident_state(incident_id, "alerted") is True
    assert incidents.get_incident(incident_id)["state"] == "alerted"


def test_resolved_without_a_reason_can_still_be_alerted(cron_home):
    """Recovery (a later green run) does not stamp resolution_reason. Only a
    reason-bearing resolution is sticky against a late alert."""
    job = create_job(prompt="recovered", schedule="every 1h", name="recovered")
    incident_id, _ = incidents.upsert_incident(job["id"], "blip")
    assert incidents.close_incidents_for_recovered_job(job["id"]) == 1
    assert incidents.get_incident(incident_id)["state"] == "resolved"
    assert not (incidents.get_incident(incident_id).get("resolution_reason") or "").strip()
    assert incidents.set_incident_state(incident_id, "alerted") is True
    assert incidents.get_incident(incident_id)["state"] == "alerted"


def test_prune_orphans_plans_then_applies_without_touching_closed(cron_home, capsys):
    live = create_job(prompt="still here", schedule="every 1h", name="live")
    live_inc, _ = incidents.upsert_incident(live["id"], "live job failure")
    orphan_id, _ = incidents.upsert_incident("44513ec15fca", "job was deleted earlier")
    closed_orphan, _ = incidents.upsert_incident("deadbeefcafe", "acked after the job vanished")
    assert incidents.ack_incident(closed_orphan) is True

    known = {live["id"]}
    planned = incidents.list_open_orphan_incidents(known)
    assert [row["id"] for row in planned] == [orphan_id]
    assert incidents.get_incident(orphan_id)["state"] == "detected"

    assert cron_incidents(argparse.Namespace(
        prune_orphans=True, apply=False, incident_action="list", incident_id=None, state=None,
    )) == 0
    plan_out = capsys.readouterr().out
    assert orphan_id in plan_out
    assert "--apply" in plan_out
    assert incidents.get_incident(orphan_id)["state"] == "detected"

    assert cron_incidents(argparse.Namespace(
        prune_orphans=True, apply=True, incident_action="list", incident_id=None, state=None,
    )) == 0
    applied = incidents.get_incident(orphan_id)
    assert applied["state"] == "resolved"
    assert applied["resolution_reason"] == "job no longer exists"
    assert incidents.get_incident(live_inc)["state"] == "detected"
    assert incidents.get_incident(closed_orphan)["state"] == "closed"


def test_doctor_reports_orphans_without_resolving_or_creating_a_ledger(cron_home, tmp_path, monkeypatch, capsys):
    orphan_id, _ = incidents.upsert_incident("44513ec15fca", "orphan stays open")
    assert cron_doctor(argparse.Namespace(prune=False)) == 1
    out = capsys.readouterr().out
    assert "hermes cron incidents --prune-orphans" in out
    assert incidents.get_incident(orphan_id)["state"] == "detected"

    absent = tmp_path / "no-ledger" / "executions.db"
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", absent)
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", absent)
    assert incidents.list_open_orphan_incidents(set()) == []
    assert not absent.exists()
    assert not absent.parent.exists()
