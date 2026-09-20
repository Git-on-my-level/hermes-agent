"""Concurrent completion owners cannot double-spawn or overrun the origin cap."""
from concurrent.futures import ThreadPoolExecutor

from hermes_state import SessionDB
from hermes_state_continuation import save_checkpoint, claim_continuation, read_checkpoint


def test_claim_is_atomic_and_durable_across_independent_connections(tmp_path):
    path = tmp_path / "state.db"
    a, b = SessionDB(db_path=path), SessionDB(db_path=path)
    try:
        a.create_session("root", "telegram", chat_id="owner", thread_id="topic", cwd=str(tmp_path))
        save_checkpoint(a, "root", "Completed step A; uncommitted report.md; next step B", "iteration_cap")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda db: claim_continuation(db, "root", 1), (a, b)))
        assert sorted(c.status for c in claims) == ["claimed", "spawn"]
        child = next(c for c in claims if c.status == "spawn")
        assert "report.md" in child.seed
        assert b.get_session(child.session_id)["chat_id"] == "owner"
        assert b.get_session(child.session_id)["thread_id"] == "topic"
        assert b.get_session(child.session_id)["cwd"] == str(tmp_path)
        # A late watchdog checkpoint must not erase an already-admitted child.
        save_checkpoint(b, "root", "Updated progress", "cron_idle_timeout")
        assert claim_continuation(a, "root", 1).status == "claimed"
        save_checkpoint(a, child.session_id, "More work remains", "iteration_cap")
        assert claim_continuation(b, child.session_id, 1).status == "limit"
        assert read_checkpoint(b, "root")["count"] == 1
    finally:
        a.close()
        b.close()
