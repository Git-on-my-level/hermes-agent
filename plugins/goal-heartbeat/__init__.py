"""goal-heartbeat: a model-level heartbeat for active /goal sessions that went quiet.

A parked goal resumes in the gateway only when something starts a turn (normally its
waiter's exit notice). If the waiter hangs, dies without a notice, or waits on the wrong
thing, the session sits silent indefinitely: upstream's 30-minute barrier cap is applied
lazily, on the next turn, which never comes. This plugin is that turn. Every
``interval_minutes`` of session inactivity it injects a check-in, so the agent (not a
liveness probe) re-judges whether the wait is still healthy and still the right wait.

Escalation: heartbeats since the last real activity (the user, a process notice, or any
other non-heartbeat user turn) are counted from the session's own history. Number
``escalate_after`` asks the agent to tell the user what is stuck; after that, heartbeats stop
until something else happens in the session.

Config (plugins.entries.goal-heartbeat):
  allow_gateway_injection: true   # required
  interval_minutes: 50            # idle time before a heartbeat (default 50)
  escalate_after: 3               # the Nth silent heartbeat escalates (default 3)
  enabled: true                   # false stops new heartbeats without unloading

Dry run against the live DB:  python3 __init__.py --dry-run [--interval M] [--escalate N]
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
import json
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ID = "goal-heartbeat"
MARKER = "[Goal heartbeat "
POLL_SECONDS = 60.0
DEFAULTS = {"interval_minutes": 50, "escalate_after": 3, "enabled": True}
# A plugin reload re-executes this module without stopping the old thread: the owner token and the
# recent-fire map live on ``sys`` so they survive the re-import; a thread whose token is stale exits.
_TOKENS = sys.__dict__.setdefault("_hermes_plugin_thread_tokens", {})
_recent: dict = sys.__dict__.setdefault("_hermes_goal_heartbeat_recent", {})  # session_id -> injected_at


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _settings() -> dict:
    out = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
        for k in DEFAULTS:
            if k in entry:
                out[k] = entry[k]
    except Exception:
        logger.debug("goal-heartbeat: config read failed; using defaults", exc_info=True)
    out["interval_minutes"] = max(5.0, float(out["interval_minutes"]))
    out["escalate_after"] = max(1, int(out["escalate_after"]))
    return out


def _in_gateway_process() -> bool:
    try:
        from hermes_cli import plugins as host
        return getattr(host, "_published_gateway_message_injector", None) is not None
    except Exception:
        return False


def _target(goal: dict) -> str:
    if goal.get("waiting_on_session"):
        return f"session {goal['waiting_on_session']}"
    if goal.get("waiting_on_pid"):
        return f"pid {goal['waiting_on_pid']}"
    if float(goal.get("waiting_until") or 0):
        return "a timed wait"
    return "nothing (goal active, no wait barrier)"


def candidates(db_path: Path, interval_s: float, escalate_after: int, now: float):
    """Yield (session_id, session_key, k, idle_s, goal) for each heartbeat due now; k is 1-based."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        for key, raw in con.execute("select key, value from state_meta where key like 'goal:%'"):
            try:
                goal = json.loads(raw)
            except Exception:
                continue
            if goal.get("status") != "active":
                continue
            sid = key[len("goal:"):]
            row = con.execute(
                "select session_key, ended_at from sessions where id=?", (sid,)).fetchone()
            if not row or not row[0] or row[1] is not None or not str(row[0]).startswith("agent:"):
                continue
            last = con.execute("select max(timestamp) from messages where session_id=?", (sid,)).fetchone()[0]
            if not last:
                continue
            idle = now - float(last)
            if idle < interval_s:
                continue
            # Heartbeats since the last real (non-heartbeat) user turn.
            real = con.execute(
                "select coalesce(max(id), 0) from messages where session_id=? and role='user' "
                "and instr(coalesce(content,''), ?) = 0", (sid, MARKER)).fetchone()[0]
            beats = con.execute(
                "select count(*) from messages where session_id=? and role='user' and id>? "
                "and instr(coalesce(content,''), ?) > 0", (sid, real, MARKER)).fetchone()[0]
            if beats >= escalate_after:
                continue  # already escalated; wait for something real to happen
            yield sid, row[0], beats + 1, idle, goal
    finally:
        con.close()


def render(k: int, n: int, idle_s: float, goal: dict) -> str:
    head = f"{MARKER}{k}/{n} — no session activity for {int(idle_s // 60)}m; goal waiting on {_target(goal)}]"
    if k < n:
        return (f"{head}\nGoal: {goal.get('goal', '')}\n"
                "Check that what you are waiting on is still progressing and is still the right thing to "
                "wait on: the process is alive and its output is moving, the CI run/deploy/agent is actually "
                "running, nothing is parked on an approval, and the condition can still happen. "
                "If something finished, broke, or changed, act on it now. If it is healthy, make sure something "
                "will wake you (a background process with notify that exits when the condition changes), then "
                "reply exactly [SILENT].")
    return (f"{head}\nGoal: {goal.get('goal', '')}\n"
            f"This is check {k} with no progress since the last real event. Do not reply [SILENT]. Send the user "
            "one short message: what the goal is waiting on, why it has not moved, and the one thing you need "
            "from them (or your default if they do nothing). Heartbeats stop until something new happens here.")


def _tick(ctx) -> None:
    cfg = _settings()
    if not cfg["enabled"]:
        return
    interval_s = cfg["interval_minutes"] * 60
    now = time.time()
    for sid, key, k, idle, goal in candidates(_home() / "state.db", interval_s, cfg["escalate_after"], now):
        if now - _recent.get(sid, 0) < interval_s:
            continue  # injected already; queued behind a running turn or not yet persisted
        ok = False
        try:
            ok = bool(ctx.inject_message(render(k, cfg["escalate_after"], idle, goal), role="user", session_key=key))
        except Exception:
            logger.warning("goal-heartbeat: inject failed for %s", key, exc_info=True)
        if ok:
            _recent[sid] = now
            logger.info("goal-heartbeat: fired %d/%d -> %s (idle %dm)", k, cfg["escalate_after"], sid, idle // 60)


def _loop(ctx, token) -> None:
    while _TOKENS.get(PLUGIN_ID) is token:
        try:
            if _in_gateway_process():
                _tick(ctx)
        except Exception:
            logger.warning("goal-heartbeat: tick error", exc_info=True)
        time.sleep(POLL_SECONDS)


def register(ctx):
    token = object()
    _TOKENS[PLUGIN_ID] = token  # retires any thread from a previous load
    threading.Thread(target=_loop, args=(ctx, token), name=PLUGIN_ID, daemon=True).start()


if __name__ == "__main__" and "--dry-run" in sys.argv:
    args = sys.argv
    interval = float(args[args.index("--interval") + 1]) if "--interval" in args else DEFAULTS["interval_minutes"]
    n = int(args[args.index("--escalate") + 1]) if "--escalate" in args else DEFAULTS["escalate_after"]
    found = False
    for sid, key, k, idle, goal in candidates(_home() / "state.db", interval * 60, n, time.time()):
        found = True
        print(f"WOULD FIRE {k}/{n} -> {sid} ({key}) idle={int(idle // 60)}m")
        print("  " + render(k, n, idle, goal).replace("\n", "\n  "))
    if not found:
        print(f"nothing due (interval={interval}m, escalate_after={n})")
