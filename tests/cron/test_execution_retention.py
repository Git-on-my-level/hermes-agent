"""Issue #37 §2 — time-based execution retention.

The ledger's terminal history was capped by ROW COUNT only (MAX_TERMINAL_EXECUTIONS), so a
couple of 2-minute watchdogs evicted every quiet job's history and no failure rate was
computable. Retention is now primarily TIME-based (``cron.executions_retention_days``), with
the row cap kept as a growth backstop and a per-job floor (``cron.executions_min_per_job``)
so a noisy job can never starve a quiet one below a knowable minimum.

Time is simulated the way production ages rows: rows are written at the real clock, then a
LATER terminal write happens with the module clock pushed forward — that write's prune pass
compares the now-old stamps against a forward cutoff, exactly as a prune would days later.
"""

from contextlib import contextmanager
from datetime import timedelta

import pytest

from cron import executions


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


@contextmanager
def _clock_ahead(ledger, monkeypatch, days: float):
    from hermes_time import now as hermes_now

    with monkeypatch.context() as m:
        m.setattr(
            ledger, "_hermes_now", lambda: hermes_now() + timedelta(days=days))
        yield


def _finish(ledger, job_id, *, success: bool = True):
    record = ledger.create_execution(job_id, source="builtin")
    assert ledger.mark_execution_running(record["id"]) is not None
    assert ledger.finish_execution(record["id"], success=success) is not None
    return record


def _days_later(ledger, monkeypatch, days: float):
    """Return a finish helper whose writes (and prune cutoff) run ``days`` in the future."""
    def finish(job_id, *, success: bool = True):
        with _clock_ahead(ledger, monkeypatch, days):
            return _finish(ledger, job_id, success=success)

    return finish


def _terminal_rows(ledger):
    with ledger._transaction() as conn:
        return conn.execute(
            "SELECT job_id, finished_at FROM executions "
            "WHERE status IN ('completed','failed','unknown')"
        ).fetchall()


def test_time_retention_prunes_only_past_cutoff_rows(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 14.0)
    monkeypatch.setattr(ledger, "_terminal_min_per_job", lambda: 0)
    _finish(ledger, "ancient")
    _finish(ledger, "recent")
    # 30 days pass; the next terminal write's prune evicts both old rows' seniors — here both
    # 'ancient' and 'recent' are past the 14-day cutoff, so it ends with only the new row.
    finish = _days_later(ledger, monkeypatch, 30)
    finish("fresh")

    assert [r["job_id"] for r in _terminal_rows(ledger)] == ["fresh"]


def test_time_retention_keeps_unparseable_finished_at(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 14.0)
    monkeypatch.setattr(ledger, "_terminal_min_per_job", lambda: 0)
    record = _finish(ledger, "broken-stamp")
    with ledger._transaction() as conn:
        conn.execute("UPDATE executions SET finished_at='not-a-timestamp' WHERE id=?",
                     (record["id"],))
    finish = _days_later(ledger, monkeypatch, 30)
    finish("recent")

    # Never guess into deletion: an unreadable stamp survives the sweep.
    assert sorted(r["job_id"] for r in _terminal_rows(ledger)) == ["broken-stamp", "recent"]


def test_time_retention_never_touches_inflight_rows(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 14.0)
    monkeypatch.setattr(ledger, "_terminal_min_per_job", lambda: 0)
    inflight = ledger.create_execution("live", source="builtin")
    ledger.mark_execution_running(inflight["id"])
    _finish(ledger, "ancient-terminal")

    finish = _days_later(ledger, monkeypatch, 400)
    finish("fresh")

    assert ledger.get_execution(inflight["id"])["status"] == "running"
    assert [r["job_id"] for r in _terminal_rows(ledger)] == ["fresh"]


def test_per_job_floor_keeps_newest_rows_beyond_retention(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 14.0)
    monkeypatch.setattr(ledger, "_terminal_min_per_job", lambda: 2)
    for _ in range(4):
        _finish(ledger, "weekly")
    _finish(ledger, "noise")

    finish = _days_later(ledger, monkeypatch, 40)
    finish("fresh")

    rows = sorted(r["job_id"] for r in _terminal_rows(ledger))
    # weekly keeps its 2 newest past-retention rows; noise's only row is within its floor too.
    assert rows == ["fresh", "noise", "weekly", "weekly"]


def test_row_cap_still_bounds_growth_as_backstop(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 0.0)  # time pruning off
    monkeypatch.setattr(ledger, "MAX_TERMINAL_EXECUTIONS", 3)
    for index in range(8):
        _finish(ledger, f"job-{index}")

    assert len(_terminal_rows(ledger)) == 3


def test_time_pruning_disabled_by_nonpositive_retention(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 0.0)
    _finish(ledger, "ancient")

    finish = _days_later(ledger, monkeypatch, 400)
    finish("fresh")

    assert sorted(r["job_id"] for r in _terminal_rows(ledger)) == ["ancient", "fresh"]


def test_zero_floor_disables_floor_protection(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "_terminal_retention_days", lambda: 14.0)
    monkeypatch.setattr(ledger, "_terminal_min_per_job", lambda: 0)
    _finish(ledger, "weekly")

    finish = _days_later(ledger, monkeypatch, 40)
    finish("fresh")

    assert [r["job_id"] for r in _terminal_rows(ledger)] == ["fresh"]
