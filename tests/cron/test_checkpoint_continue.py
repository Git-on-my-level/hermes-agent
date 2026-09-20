"""Cron checkpoints cross watchdog, persistence, one-shot dispatch and profile boundaries."""
import concurrent.futures
import threading
from pathlib import Path

import pytest

from agent.secret_scope import set_multiplex_active
from cron.jobs import use_cron_store, load_jobs
from gateway.run import _profile_runtime_scope
from hermes_state import SessionDB
from hermes_state_continuation import read_checkpoint
from tests.agent.test_checkpoint_continue import make_agent, install_model


def test_idle_checkpoint_waits_for_worker_and_resumes_same_job(tmp_path, monkeypatch, make_cron_provider):
    import cron.scheduler as scheduler
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    from cron.continuation import continue_cron_job, cron_session_id
    from hermes_constants import get_hermes_home
    import cron.scheduler_provider as providers
    registered = []
    provider = make_cron_provider(register_job=lambda job: registered.append((get_hermes_home(), job["id"])))
    monkeypatch.setattr(providers, "resolve_cron_scheduler", lambda: provider)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    homes = [tmp_path / name for name in ("A", "B")]
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text(
            "tools:\n  tool_search:\n    enabled: false\ncontinuation:\n  enabled: true\n  max_per_origin: 1\n  soft_budget_fraction: 0.5\n", encoding="utf-8")
    set_multiplex_active(True)
    pending = []
    try:
        for index, home in enumerate((homes[0], homes[1], homes[0])):
            with _profile_runtime_scope(home), use_cron_store(home):
                db = SessionDB()
                sid = f"idle-{index}"
                agent = make_agent(home, sid, db, platform="cron")
                db.ensure_session(sid, source="cron")
                db.append_message(sid, "user", "Finish weekly report")
                db.append_message(sid, "assistant", "Completed collection. Output: report.md; next verify totals.")
                agent._todo_store.write([{"id": "1", "content": "collected report.md", "status": "completed"}])
                agent._turn_file_mutation_paths = {"report.md"}
                future = concurrent.futures.Future()
                job = {"id": "weekly", "prompt": "Finish weekly report", "deliver": "origin",
                       "origin": {"platform": "telegram", "chat_id": home.name, "thread_id": "topic"},
                       "workdir": str(home), "script": "already-ran.py"}
                # Exercise the actual watchdog terminal path, not a provider TimeoutError.
                try:
                    scheduler._raise_inactivity_timeout(agent, "weekly", 600)
                except TimeoutError:
                    assert agent._cron_idle_timed_out
                notice = continue_cron_job(job, agent, job["prompt"], idle=True, future=future)
                assert "after the idle worker exits" in notice
                assert not any(j["id"] == f"{sid}-cont-1" for j in load_jobs())
                handoff = read_checkpoint(db, sid)["handoff"]
                assert "Finish weekly report" in handoff and "collected report.md" in handoff
                assert "report.md" in handoff
                pending.append((home, future, db, agent, sid))
        # Finish from an unscoped thread, just like a late Future completion. Each
        # callback must rebind A/B/A instead of borrowing the last caller's profile.
        for home, future, db, agent, sid in pending:
            thread = threading.Thread(target=future.set_result, args=({"interrupted": True},))
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            with _profile_runtime_scope(home), use_cron_store(home):
                assert any(j["id"] == f"{sid}-cont-1" for j in load_jobs()), (db.db_path, home, list(home.rglob("jobs.json")), load_jobs())
                child = next(j for j in load_jobs() if j["id"] == f"{sid}-cont-1")
                assert (home, child["id"]) in registered
                assert child["origin"]["chat_id"] == home.name
                assert child["origin"]["thread_id"] == "topic" and child["workdir"] == str(home)
                assert child["script"] is None
                assert cron_session_id(child, "unused") == child["id"]
                assert "collected report.md" in child["prompt"]
                resumed = make_agent(home, child["id"], db, platform="cron")
                install_model(resumed)
                result = resumed.run_conversation(child["prompt"])
                assert result["continuation_ready"]
                assert "limit reached" in continue_cron_job(child, resumed, child["prompt"])
                assert len([j for j in load_jobs() if j["id"].startswith(sid)]) == 1
                resumed._end_session_on_close = False
                resumed.close()
    finally:
        for home, future, db, agent, sid in pending:
            with _profile_runtime_scope(home):
                agent._end_session_on_close = False
                agent.close()
                db.close()
        set_multiplex_active(False)


@pytest.mark.parametrize("idle", [False, True])
def test_run_job_queues_budget_continuation_and_executes_reserved_session(tmp_path, monkeypatch, idle):
    import cron.scheduler as scheduler
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    from cron.jobs import create_job
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "tools:\n  tool_search:\n    enabled: false\ncontinuation:\n  enabled: true\n  max_per_origin: 1\n  soft_budget_fraction: 0.5\n", encoding="utf-8")
    # Resolve only the external inference route in-process; production config loading,
    # prompt construction, agent loop, watchdog, DB, queue and teardown all run.
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *a: scheduler._CronAgentSetup(
        model="test/model", runtime={"provider": "openai-compat"}, max_iterations=5))
    made = []
    release, scheduled, stalled = threading.Event(), threading.Event(), threading.Event()
    import cron.continuation as continuation
    real_enqueue = continuation.enqueue_continuation
    def record_enqueue(job, claim):
        child = real_enqueue(job, claim)
        scheduled.set()
        return child
    monkeypatch.setattr(continuation, "enqueue_continuation", record_enqueue)
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 0.1 if idle else 0)
    def construct(_cls, job, cfg, setup, *, workdir, session_id, session_db):
        agent = make_agent(tmp_path, session_id, session_db, platform="cron")
        requests = install_model(agent)
        if idle and not made:
            normal_call = agent._interruptible_streaming_api_call
            def stall(kwargs, **options):
                if len(requests) == 1:
                    stalled.set()
                    assert release.wait(timeout=25), "test did not release idle worker"
                return normal_call(kwargs, **options)
            agent._interruptible_streaming_api_call = stall
            activity = agent.get_activity_summary
            agent.get_activity_summary = lambda: dict(activity(), seconds_since_activity=601 if stalled.is_set() else 0)
        made.append(session_id)
        return agent
    monkeypatch.setattr(scheduler, "_construct_cron_agent", construct)
    with _profile_runtime_scope(tmp_path), use_cron_store(tmp_path):
        job = create_job("Finish weekly report", "1h", deliver="local", workdir=str(tmp_path))
        try:
            success, output, final, error = scheduler.run_job(job)
            assert success is (not idle) and "will continue" in final
            if idle:
                assert stalled.is_set()
                assert not scheduled.is_set()
        finally:
            release.set()
        assert scheduled.wait(timeout=15)
        assert any(j.get("continuation_session_id") for j in load_jobs()), (final, list(tmp_path.rglob("jobs.json")), load_jobs())
        child = next(j for j in load_jobs() if j.get("continuation_session_id"))
        assert child["id"] == made[0] + "-cont-1"
        success, output, final, error = scheduler.run_job(child)
        assert success and "limit reached" in final
        assert made[1] == child["id"]
        assert len([j for j in load_jobs() if j.get("continuation_session_id")]) == 1
        (tmp_path / "config.yaml").write_text("continuation:\n  enabled: false\n", encoding="utf-8")
        with pytest.raises(ValueError, match="disabled"):
            scheduler.run_job(child)
        assert len(made) == 2
