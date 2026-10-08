"""goal-heartbeat: fires only for idle active-goal gateway sessions, counts, escalates, resets."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "goal-heartbeat"
NOW = 1_800_000_000.0


def _load(name="goal_heartbeat_under_test"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript("""
        create table state_meta (key text primary key, value text);
        create table sessions (id text primary key, session_key text, ended_at real,
                               parent_session_id text, end_reason text, last_activity_at real);
        create table session_turn_leases (conversation_id text primary key, holder text,
                                          acquired_at real, expires_at real);
        create table messages (id integer primary key autoincrement, session_id text, role text,
                               content text, timestamp real);
    """)
    con.commit()
    return path, con


def _goal(con, sid, status="active", key="agent:main:telegram:dm:1", ended=None, **wait):
    goal = {"goal": f"ship {sid}", "status": status, **wait}
    con.execute("insert into state_meta values (?, ?)", (f"goal:{sid}", json.dumps(goal)))
    con.execute("insert into sessions (id, session_key, ended_at) values (?, ?, ?)", (sid, key, ended))
    con.commit()


def _msg(con, sid, role, content, ts):
    con.execute("insert into messages (session_id, role, content, timestamp) values (?, ?, ?, ?)",
                (sid, role, content, ts))
    con.commit()


def _due(mod, path, interval_min=50, n=3, now=NOW):
    return {sid: (k, goal) for sid, _key, k, _idle, goal in mod.candidates(path, interval_min * 60, n, now)}


def test_only_idle_active_gateway_goals_are_due(db):
    path, con = db
    mod = _load()
    _goal(con, "idle", waiting_on_session="proc_1")
    _msg(con, "idle", "user", "go", NOW - 3600)
    _goal(con, "busy", waiting_on_pid=42)
    _msg(con, "busy", "assistant", "working", NOW - 600)
    _goal(con, "paused", status="paused")
    _msg(con, "paused", "user", "go", NOW - 7200)
    _goal(con, "ended", ended=NOW - 100)
    _msg(con, "ended", "user", "go", NOW - 7200)
    _goal(con, "cli", key="cli-session")
    _msg(con, "cli", "user", "go", NOW - 7200)
    due = _due(mod, path)
    assert set(due) == {"idle"}
    assert due["idle"][0] == 1


def test_count_escalates_then_stops_until_a_real_event(db):
    path, con = db
    mod = _load()
    _goal(con, "s", waiting_on_session="proc_1")
    _msg(con, "s", "user", "[David] ship it", NOW - 9 * 3600)
    for k in (1, 2, 3):
        assert _due(mod, path)["s"][0] == k
        _msg(con, "s", "user", "[David] " + mod.render(k, 3, 3600, {"goal": "g"}), NOW - 8 * 3600 + k)
        _msg(con, "s", "assistant", "[SILENT]", NOW - 8 * 3600 + k)
    assert "s" not in _due(mod, path)  # escalated: wait for something real
    _msg(con, "s", "user", "[David] [IMPORTANT: Background process proc_1 completed]", NOW - 7200)
    assert _due(mod, path)["s"][0] == 1


def test_goal_continuations_do_not_reset_the_count(db):
    path, con = db
    mod = _load()
    _goal(con, "s", waiting_on_session="proc_1")
    _msg(con, "s", "user", "ship it", NOW - 9 * 3600)
    _msg(con, "s", "user", mod.render(1, 3, 3600, {"goal": "g"}), NOW - 8 * 3600)
    _msg(con, "s", "user", "[Continuing toward your standing goal] take one concrete step", NOW - 8 * 3600 + 1)
    _msg(con, "s", "user", "[Heartbeat — recurring instruction, fires every 1h]", NOW - 8 * 3600 + 2)
    assert _due(mod, path)["s"][0] == 2


def test_running_turn_or_fresh_activity_blocks_a_heartbeat(db):
    path, con = db
    mod = _load()
    _goal(con, "s", waiting_on_session="proc_1")
    _msg(con, "s", "user", "go", NOW - 3 * 3600)
    con.execute("insert into sessions (id, session_key, ended_at, end_reason) values ('root', 'agent:x', ?, 'compression')",
                (NOW - 4 * 3600,))
    con.execute("update sessions set parent_session_id='root' where id='s'")
    con.execute("insert into session_turn_leases values ('root', 'pid=1', ?, ?)", (NOW - 60, NOW + 240))
    con.commit()
    assert "s" not in _due(mod, path)  # mid-turn: the lease is keyed by the compression root
    con.execute("delete from session_turn_leases")
    con.execute("update sessions set last_activity_at=? where id='s'", (NOW - 120,))
    con.commit()
    assert "s" not in _due(mod, path)  # long turn still heart-beating activity
    con.execute("update sessions set last_activity_at=? where id='s'", (NOW - 3 * 3600,))
    con.commit()
    assert "s" in _due(mod, path)


def test_render_escalates_on_the_last_check():
    mod = _load()
    goal = {"goal": "merge PR", "waiting_on_pid": 7}
    normal, last = mod.render(1, 3, 3000, goal), mod.render(3, 3, 3000, goal)
    assert normal.startswith(mod.MARKER) and "pid 7" in normal and "exactly [SILENT]" in normal
    assert "Do not reply [SILENT]" in last and "Send the user" in last


def test_settings_defaults_and_clamps(monkeypatch):
    mod = _load()
    import hermes_cli.config as config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"plugins": {"entries": {
        "goal-heartbeat": {"interval_minutes": 1, "escalate_after": 0}}}})
    s = mod._settings()
    assert s["interval_minutes"] == mod.MIN_INTERVAL_MINUTES == 15.0
    assert s["escalate_after"] == 1 and s["enabled"] is True


class Ctx:
    def __init__(self):
        self.calls = []

    def inject_message(self, content, role="user", *, session_key=None):
        self.calls.append((session_key, content))
        return True


def test_tick_injects_once_then_retries_a_dropped_dispatch(db, monkeypatch):
    path, con = db
    mod = _load()
    mod._recent.clear()
    _goal(con, "s", waiting_on_session="proc_1")
    _msg(con, "s", "user", "go", time.time() - 3 * 3600)
    monkeypatch.setattr(mod, "_settings", lambda: dict(mod.DEFAULTS, interval_minutes=50.0))
    ctx = Ctx()
    mod._tick(ctx, path.parent)
    mod._tick(ctx, path.parent)  # not yet visible in the session: held back
    assert len(ctx.calls) == 1 and ctx.calls[0][0] == "agent:main:telegram:dm:1"
    assert ctx.calls[0][1].startswith(f"{mod.MARKER}1/3")
    later = mod._recent[(str(path.parent), "s")] + mod.RETRY_SECONDS + 1
    monkeypatch.setattr(mod.time, "time", lambda: later)
    mod._tick(ctx, path.parent)  # never landed: the gateway dropped it, so retry
    assert len(ctx.calls) == 2


def _start_loop(mod, home, monkeypatch, enabled=True):
    monkeypatch.setattr(mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(mod, "_plugin_still_enabled", lambda: enabled)
    monkeypatch.setattr(mod, "_in_gateway_process", lambda: False)
    key, token = f"{mod.PLUGIN_ID}:{home}", object()
    mod._TOKENS[key] = token
    t = threading.Thread(target=mod._loop, args=(Ctx(), key, token, home), daemon=True)
    t.start()
    return t, key


def test_reload_retires_the_previous_heartbeat_thread(tmp_path, monkeypatch):
    first = _load("gh_first")
    t, key = _start_loop(first, tmp_path, monkeypatch)
    time.sleep(0.05)
    assert t.is_alive()
    second = _load("gh_second")
    assert second._recent is first._recent
    second._TOKENS[key] = object()
    t.join(timeout=2)
    assert not t.is_alive()


def test_disabled_plugin_thread_exits(tmp_path, monkeypatch):
    mod = _load("gh_disabled")
    t, _ = _start_loop(mod, tmp_path, monkeypatch, enabled=False)
    t.join(timeout=2)
    assert not t.is_alive()
