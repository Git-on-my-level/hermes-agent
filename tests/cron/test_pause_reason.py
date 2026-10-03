"""Pausing a cron job requires a non-empty reason. Existing pauses with none still do not fire."""

from __future__ import annotations

import argparse
import json

import pytest

from cron.jobs import (
    claim_job_for_fire,
    create_job,
    get_due_jobs,
    get_job,
    load_jobs,
    pause_job,
    resume_job,
    save_jobs,
    update_job,
)
from hermes_cli.cron import cron_doctor, cron_pause
from tools.cronjob_tools import cronjob


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_pause_rejects_blank_reason_and_stores_a_real_one(tmp_cron_dir):
    job = create_job(prompt="stay quiet", schedule="every 1h", name="watchdog")

    with pytest.raises(ValueError, match="non-empty reason"):
        pause_job(job["id"])
    with pytest.raises(ValueError, match="non-empty reason"):
        pause_job(job["id"], reason="   ")

    paused = pause_job(job["id"], reason="  provider migration  ", review_after="2099-06-01")
    assert paused["state"] == "paused"
    assert paused["enabled"] is False
    assert paused["paused_reason"] == "provider migration"
    assert paused["paused_at"]
    assert paused["paused_review_after"] == "2099-06-01"
    assert get_job(job["id"])["paused_reason"] == "provider migration"


def test_existing_pause_without_reason_stays_dark(tmp_cron_dir):
    """Jobs paused before the reason requirement keep their state and do not fire."""
    job = create_job(prompt="legacy pause", schedule="every 1h", name="legacy")
    stored = load_jobs()
    for row in stored:
        if row["id"] == job["id"]:
            row["enabled"] = False
            row["state"] = "paused"
            row["paused_at"] = "2026-09-01T00:00:00+00:00"
            row["paused_reason"] = None
            row["next_run_at"] = "2020-01-01T00:00:00+00:00"
    save_jobs(stored)

    loaded = get_job(job["id"])
    assert loaded["state"] == "paused"
    assert not (loaded.get("paused_reason") or "").strip()
    assert job["id"] not in {due["id"] for due in get_due_jobs()}
    assert not claim_job_for_fire(job["id"])


def test_doctor_names_how_to_add_a_reason_and_flags_a_due_review(tmp_cron_dir, capsys):
    blank = create_job(prompt="no reason", schedule="every 1h", name="blank-reason")
    future = create_job(prompt="later", schedule="every 1h", name="future-review")
    due = create_job(prompt="review me", schedule="every 1h", name="due-review")
    rows = load_jobs()
    for row in rows:
        row["enabled"] = False
        row["state"] = "paused"
        row["paused_at"] = "2026-09-01T00:00:00+00:00"
        if row["id"] == blank["id"]:
            row["paused_reason"] = ""
        elif row["id"] == future["id"]:
            row["paused_reason"] = "waiting on a migration"
            row["paused_review_after"] = "2099-01-01"
        else:
            row["paused_reason"] = "check the watchdog"
            row["paused_review_after"] = "2020-01-01"
    save_jobs(rows)

    assert cron_doctor(argparse.Namespace(prune=False)) == 1
    out = capsys.readouterr().out
    assert f'hermes cron edit {blank["id"]} --paused-reason' in out
    assert "2099-01-01" not in out
    assert "2020-01-01" in out
    assert f"hermes cron edit {due['id']} --paused-review-after" in out


def test_resume_clears_reason_and_review_date(tmp_cron_dir):
    job = create_job(prompt="come back", schedule="every 1h")
    pause_job(job["id"], reason="hold", review_after="2099-01-02")
    resumed = resume_job(job["id"])
    assert resumed["state"] == "scheduled"
    assert resumed.get("paused_reason") is None
    assert resumed.get("paused_review_after") is None


def test_edit_can_attach_a_reason_to_an_existing_pause(tmp_cron_dir):
    job = create_job(prompt="annotate", schedule="every 1h")
    rows = load_jobs()
    for row in rows:
        if row["id"] == job["id"]:
            row["enabled"] = False
            row["state"] = "paused"
            row["paused_reason"] = None
    save_jobs(rows)

    updated = update_job(job["id"], {"paused_reason": "  found the intent  "})
    assert updated["paused_reason"] == "found the intent"
    assert updated["state"] == "paused"
    with pytest.raises(ValueError, match="non-empty reason"):
        update_job(job["id"], {"paused_reason": "  "})


def test_cli_and_tool_reject_an_empty_pause_reason(tmp_cron_dir, capsys):
    job = create_job(prompt="via cli", schedule="every 1h", name="cli-pause")
    assert cron_pause(argparse.Namespace(job_id=job["id"], reason="   ", review_after=None)) == 1
    assert get_job(job["id"])["state"] != "paused"

    refused = json.loads(cronjob(action="pause", job_id=job["id"], reason=""))
    assert refused["success"] is False

    assert cron_pause(argparse.Namespace(
        job_id=job["id"], reason="maintenance window", review_after="2099-04-04")) == 0
    stored = get_job(job["id"])
    assert stored["paused_reason"] == "maintenance window"
    assert stored["paused_review_after"] == "2099-04-04"
    assert "Reason: maintenance window" in capsys.readouterr().out
