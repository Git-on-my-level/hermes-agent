"""session-inject: drain dispatches exactly once, confirms against the session, survives reloads."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "session-inject"


def _load(name="session_inject_under_test"):
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state" / "inject-spool").mkdir(parents=True)
    return tmp_path


def _db(home):
    con = sqlite3.connect(home / "state.db")
    con.executescript("""
        create table if not exists sessions (id text primary key, session_key text, ended_at real, title text);
        create table if not exists messages (id integer primary key autoincrement, session_id text,
                                             role text, content text, timestamp real);
    """)
    return con


class Recorder:
    def __init__(self, ok=True):
        self.ok, self.calls, self.lock = ok, [], threading.Lock()

    def inject_message(self, content, role="user", *, session_key=None):
        with self.lock:
            self.calls.append((session_key, content, role))
        time.sleep(0.005)
        return self.ok


def test_concurrent_drains_dispatch_each_request_exactly_once(home):
    mod = _load()
    spool = mod.spool_dir(home)
    for i in range(25):
        (spool / f"{i:03d}.json").write_text(json.dumps({"session_key": "agent:k", "content": f"m{i}"}))
    rec = Recorder()
    threads = [threading.Thread(target=mod.drain_once, args=(rec, spool)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(c for _, c, _ in rec.calls) == sorted(f"m{i}" for i in range(25))
    assert len(list((spool / "sent").glob("*.json"))) == 25
    assert not list(spool.glob("*.json")) and not list(spool.glob("*.inflight"))


def test_dispatch_is_confirmed_only_when_the_session_persists_it(home):
    mod = _load()
    spool = mod.spool_dir(home)
    con = _db(home)
    con.execute("insert into sessions values ('s1', 'agent:k', null, 't')")
    con.commit()
    (spool / "a.json").write_text(json.dumps({"session_key": "agent:k", "content": "continue the deploy"}))
    mod.drain_once(Recorder(), spool)
    sent_at = json.loads((spool / "sent" / "a.json").read_text())["sent_at"]
    mod.confirm_sent(spool, home / "state.db", now=sent_at + 10)
    assert (spool / "sent" / "a.json").exists()  # dispatched, not yet seen
    con.execute("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                ("s1", "user", "[David] continue the deploy", sent_at + 1))
    con.commit()
    mod.confirm_sent(spool, home / "state.db", now=sent_at + 20)
    assert json.loads((spool / "done" / "a.json").read_text())["confirmed_at"] == sent_at + 20


def test_dispatch_the_gateway_dropped_fails_after_the_confirm_window(home):
    mod = _load()
    spool = mod.spool_dir(home)
    _db(home).close()
    (spool / "a.json").write_text(json.dumps({"session_key": "agent:gone", "content": "hello"}))
    mod.drain_once(Recorder(), spool)
    sent_at = json.loads((spool / "sent" / "a.json").read_text())["sent_at"]
    mod.confirm_sent(spool, home / "state.db", now=sent_at + mod.CONFIRM_SECONDS + 1)
    assert "never observed" in json.loads((spool / "failed" / "a.json").read_text())["error"]


def test_refused_request_is_requeued_then_failed_after_max_attempts(home, monkeypatch):
    mod = _load()
    spool = mod.spool_dir(home)
    (spool / "x.json").write_text(json.dumps({"session_key": "agent:k", "content": "hi"}))
    mod.drain_once(Recorder(ok=False), spool)
    assert json.loads((spool / "x.json").read_text())["attempts"] == 1
    monkeypatch.setattr(mod, "MAX_ATTEMPTS", 2)
    mod.drain_once(Recorder(ok=False), spool)
    assert not (spool / "x.json").exists()
    assert json.loads((spool / "failed" / "x.json").read_text())["attempts"] == 2


def test_unreadable_request_moves_to_failed(home):
    mod = _load()
    spool = mod.spool_dir(home)
    (spool / "bad.json").write_text("{not json")
    mod.drain_once(Recorder(), spool)
    assert (spool / "failed" / "bad.json").exists()


def test_stale_inflight_claim_is_returned_to_the_queue(home):
    mod = _load()
    spool = mod.spool_dir(home)
    stale, fresh = spool / "old.inflight", spool / "new.inflight"
    stale.write_text("{}")
    fresh.write_text("{}")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    assert mod.recover_stale_inflight(spool) == 1
    assert (spool / "old.json").exists() and fresh.exists()


def _start_loop(mod, home, monkeypatch, enabled=True):
    monkeypatch.setattr(mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(mod, "_plugin_still_enabled", lambda: enabled)
    key, token = f"{mod.PLUGIN_ID}:{home}", object()
    mod._TOKENS[key] = token
    t = threading.Thread(target=mod._loop, args=(Recorder(), key, token, home), daemon=True)
    t.start()
    return t, key


def test_reload_retires_the_previous_drain_thread(home, monkeypatch):
    first = _load("si_first")
    t, key = _start_loop(first, home, monkeypatch)
    time.sleep(0.05)
    assert t.is_alive()
    second = _load("si_second")  # a reload re-executes the module
    assert second._TOKENS is first._TOKENS
    second._TOKENS[key] = object()
    t.join(timeout=2)
    assert not t.is_alive()


def test_disabled_plugin_thread_exits(home, monkeypatch):
    mod = _load("si_disabled")
    t, _ = _start_loop(mod, home, monkeypatch, enabled=False)
    t.join(timeout=2)
    assert not t.is_alive()


def test_cli_queues_a_resolved_session_key(home):
    mod = _load("si_cli_pkg")
    spec = importlib.util.spec_from_file_location("si_cli_pkg.cli", PLUGIN_DIR / "cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    con = _db(home)
    con.execute("insert into sessions values ('s1', 'agent:main:telegram:dm:1', null, 't')")
    con.commit()
    con.close()
    assert cli.handle(SimpleNamespace(list_stalled=False, session="s1", message="continue", hours=6)) == 0
    (queued,) = list(mod.spool_dir(home).glob("*.json"))
    assert json.loads(queued.read_text())["session_key"] == "agent:main:telegram:dm:1"
    with pytest.raises(SystemExit):
        cli.handle(SimpleNamespace(list_stalled=False, session="missing", message="x", hours=6))


def test_register_wires_the_cli_command_through_the_package(home, monkeypatch):
    mod = _load("si_register_pkg")
    monkeypatch.setattr(mod, "_loop", lambda *a: None)
    seen = {}
    ctx = SimpleNamespace(register_cli_command=lambda name, help, setup, handle, description="": seen.update(
        name=name, setup=setup, handle=handle))
    mod.register(ctx)
    assert seen["name"] == "inject" and callable(seen["setup"]) and callable(seen["handle"])
