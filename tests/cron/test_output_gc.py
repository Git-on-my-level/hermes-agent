"""Issue #37 §3 — output GC on the retention sweep + ``prune_orphan_output``.

Deleting a job's RECORD used to leave ``cron/output/<id>/`` behind forever: the manual
delete path rmtree's the directory, but the retention sweep (and any pre-GC accumulation)
never did. The sweep now removes the directory with the record, and ``prune_orphan_output``
reaps already-orphaned directories when the operator asks (``hermes cron doctor --prune``).
"""

import pytest

from cron import jobs


@pytest.fixture
def store(monkeypatch, tmp_path):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    return jobs


def _completed_oneshot(store, monkeypatch, name, *, finished_days_ago: float):
    from datetime import timedelta
    from hermes_time import now as hermes_now

    job = store.create_job(prompt="once", schedule=hermes_now().isoformat(), name=name)
    records = store.load_jobs()
    record = next(r for r in records if r["id"] == job["id"])
    record["state"] = "completed"
    record["last_run_at"] = (hermes_now() - timedelta(days=finished_days_ago)).isoformat()
    store.save_jobs(records)
    return record


def _write_output(store, job_id, file_name="out.md", content="x"):
    out_dir = store.OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / file_name).write_text(content)
    return out_dir


def test_sweep_removes_output_dir_with_the_record(store, monkeypatch):
    monkeypatch.setattr(jobs, "COMPLETED_ONESHOT_RETENTION_DAYS", 7.0)
    aged = _completed_oneshot(store, monkeypatch, "aged", finished_days_ago=30)
    young = _completed_oneshot(store, monkeypatch, "young", finished_days_ago=1)
    _write_output(store, aged["id"])
    _write_output(store, young["id"])
    from hermes_time import now as hermes_now

    removed_ids: set = set()
    raw_jobs = store.load_jobs()
    assert store._sweep_completed_oneshots(
        raw_jobs, hermes_now(), removed_ids=removed_ids) is True
    store.save_jobs(raw_jobs, removed_ids=removed_ids)

    assert not (store.OUTPUT_DIR / aged["id"]).exists()
    assert (store.OUTPUT_DIR / young["id"]).exists()


def test_sweep_survives_output_dir_removal_failure(store, monkeypatch):
    monkeypatch.setattr(jobs, "COMPLETED_ONESHOT_RETENTION_DAYS", 7.0)
    aged = _completed_oneshot(store, monkeypatch, "aged", finished_days_ago=30)
    _write_output(store, aged["id"])
    from hermes_time import now as hermes_now

    real_rmtree = jobs.shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if path == store.OUTPUT_DIR / aged["id"]:
            raise OSError("locked")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(jobs.shutil, "rmtree", failing_rmtree)
    raw_jobs = store.load_jobs()
    # The sweep still reports the record removed; only the dir cleanup failed quietly.
    assert store._sweep_completed_oneshots(raw_jobs, hermes_now()) is True


def test_prune_orphan_output_reaps_only_unknown_ids(store, monkeypatch):
    live = _completed_oneshot(store, monkeypatch, "live", finished_days_ago=0)
    _write_output(store, live["id"])
    _write_output(store, "orphan-a")
    _write_output(store, "orphan-b", file_name="diag.md")

    removed = store.prune_orphan_output({live["id"]})

    assert [job_id for job_id, _ in removed] == ["orphan-a", "orphan-b"]
    assert (store.OUTPUT_DIR / live["id"]).exists()
    assert not (store.OUTPUT_DIR / "orphan-a").exists()
    assert not (store.OUTPUT_DIR / "orphan-b").exists()


def test_prune_orphan_output_counts_files(store):
    _write_output(store, "orphan", file_name="a.md")
    _write_output(store, "orphan", file_name="b.md")

    assert store.prune_orphan_output(set()) == [("orphan", 2)]


def test_prune_orphan_output_reports_failure_as_minus_one(store, monkeypatch):
    _write_output(store, "orphan")

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("busy")

    monkeypatch.setattr(jobs.shutil, "rmtree", failing_rmtree)
    assert store.prune_orphan_output(set()) == [("orphan", -1)]


def test_prune_orphan_output_without_output_dir(store):
    assert store.prune_orphan_output(set()) == []
