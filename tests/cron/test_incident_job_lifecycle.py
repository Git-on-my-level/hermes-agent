"""Open incidents resolve when their job is removed or finishes, and orphans can be pruned."""

from __future__ import annotations

import argparse

import pytest

import cron.incidents as incidents
from cron.jobs import create_job, get_job, mark_job_run, remove_job
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


def test_repeat_budget_and_oneshot_completion_resolve_incidents(cron_home):
    budget = create_job(prompt="finite", schedule="every 1h", name="budget", repeat=1)
    budget_inc, _ = incidents.upsert_incident(budget["id"], "still failing")
    assert mark_job_run(budget["id"], success=False, error="still failing") is True
    assert get_job(budget["id"])["state"] == "completed"
    resolved = incidents.get_incident(budget_inc)
    assert resolved["state"] == "resolved"
    assert resolved["resolution_reason"] == "repeat budget exhausted"

    once = create_job(prompt="once", schedule="in 2h", name="oneshot")
    once_inc, _ = incidents.upsert_incident(once["id"], "oneshot failed")
    assert mark_job_run(once["id"], success=False, error="oneshot failed") is True
    assert get_job(once["id"])["state"] == "completed"
    assert incidents.get_incident(once_inc)["resolution_reason"] == "job completed"
    assert incidents.get_incident(once_inc)["state"] == "resolved"


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
