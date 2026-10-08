"""session-inject: spool drain delivers exactly once, requeues refusals, and survives reloads."""
from __future__ import annotations

import importlib.util
import json
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


class Recorder:
    def __init__(self, ok=True):
        self.ok, self.calls, self.lock = ok, [], threading.Lock()

    def inject_message(self, content, role="user", *, session_key=None):
        with self.lock:
            self.calls.append((session_key, content, role))
        time.sleep(0.005)
        return self.ok


def test_concurrent_drains_deliver_each_request_exactly_once(home):
    mod = _load()
    spool = mod.spool_dir()
    for i in range(25):
        (spool / f"{i:03d}.json").write_text(json.dumps({"session_key": "agent:k", "content": f"m{i}"}))
    rec = Recorder()
    threads = [threading.Thread(target=mod.drain_once, args=(rec,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    contents = [c for _, c, _ in rec.calls]
    assert sorted(contents) == sorted(f"m{i}" for i in range(25))
    assert len(list((spool / "done").glob("*.json"))) == 25
    assert not list(spool.glob("*.json")) and not list(spool.glob("*.inflight"))


def test_refused_request_is_requeued_then_failed_after_max_attempts(home, monkeypatch):
    mod = _load()
    spool = mod.spool_dir()
    (spool / "x.json").write_text(json.dumps({"session_key": "agent:k", "content": "hi"}))
    mod.drain_once(Recorder(ok=False))
    assert json.loads((spool / "x.json").read_text())["attempts"] == 1
    monkeypatch.setattr(mod, "MAX_ATTEMPTS", 2)
    mod.drain_once(Recorder(ok=False))
    assert not (spool / "x.json").exists()
    assert json.loads((spool / "failed" / "x.json").read_text())["attempts"] == 2


def test_unreadable_request_moves_to_failed(home):
    mod = _load()
    spool = mod.spool_dir()
    (spool / "bad.json").write_text("{not json")
    mod.drain_once(Recorder())
    assert (spool / "failed" / "bad.json").exists()


def test_reload_retires_the_previous_drain_thread(home, monkeypatch):
    first = _load("si_first")
    monkeypatch.setattr(first, "POLL_SECONDS", 0.01)
    token = object()
    first._TOKENS[first._TOKEN_KEY] = token
    t = threading.Thread(target=first._loop, args=(Recorder(), token), daemon=True)
    t.start()
    second = _load("si_second")  # a reload re-executes the module
    assert second._TOKENS is first._TOKENS
    second._TOKENS[second._TOKEN_KEY] = object()
    t.join(timeout=2)
    assert not t.is_alive()


def test_cli_queues_a_resolved_session_key(home, capsys):
    mod = _load("si_cli_pkg")
    cli = sys.modules["si_cli_pkg.cli"] if "si_cli_pkg.cli" in sys.modules else None
    if cli is None:
        spec = importlib.util.spec_from_file_location("si_cli_pkg.cli", PLUGIN_DIR / "cli.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
    con = sqlite3.connect(home / "state.db")
    con.execute("create table sessions (id text primary key, session_key text, ended_at real, title text)")
    con.execute("insert into sessions values ('s1', 'agent:main:telegram:dm:1', null, 't')")
    con.commit()
    con.close()
    assert cli.handle(SimpleNamespace(list_stalled=False, session="s1", message="continue", hours=6)) == 0
    (queued,) = list(mod.spool_dir().glob("*.json"))
    assert json.loads(queued.read_text())["session_key"] == "agent:main:telegram:dm:1"
    with pytest.raises(SystemExit):
        cli.handle(SimpleNamespace(list_stalled=False, session="missing", message="x", hours=6))
