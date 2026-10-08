"""goal-heartbeat: a model-level heartbeat for active /goal sessions that went quiet.

A parked goal resumes in the gateway only when something starts a turn (normally its
waiter's exit notice). If the waiter hangs, dies without a notice, or waits on the wrong
thing, the session sits silent indefinitely: upstream's 30-minute barrier cap is applied
lazily, on the next turn, which never comes. This plugin is that turn. Every
``interval_minutes`` of session inactivity it injects a check-in, so the agent (not a
liveness probe) re-judges whether the wait is still healthy and still the right wait.

For a parked goal, a healthy check-in ends with exactly ``[SILENT]``; while the wait barrier
still holds the gateway then leaves the goal untouched (no judge call, no turn spent, no status
line). An active goal that is not parked gets no silent option: nothing is driving it, so the
check-in asks for the next concrete step (a normal judged turn).

Escalation: heartbeats since the last real event (a user message, a process notice; not a
heartbeat or a goal continuation) are counted from the session's own history. Number
``escalate_after`` asks the agent to tell the user what is stuck; after that, heartbeats stop
until something real happens in the session.

Config (plugins.entries.goal-heartbeat, re-read every minute):
  allow_gateway_injection: true   # required
  interval_minutes: 50            # idle time before a heartbeat (default 50, min 15)
  escalate_after: 3               # the Nth heartbeat without a real event escalates (default 3)
  enabled: true                   # false stops new heartbeats without unloading

Dry run against a home's live DB:  python3 __init__.py --dry-run [--interval M] [--escalate N]
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ID = "goal-heartbeat"
MARKER = "[Goal heartbeat "
# User rows that are not real events: our own check-ins, goal-loop continuations, upstream /heartbeat.
SYNTHETIC_PREFIXES = (MARKER, "[Continuing toward your standing goal", "[Heartbeat")
POLL_SECONDS = 60.0
MIN_INTERVAL_MINUTES = 15.0
RETRY_SECONDS = 600.0  # an injection that never shows up in the session is retried after this
DEFAULTS = {"interval_minutes": 50, "escalate_after": 3, "enabled": True}
# A plugin reload re-executes this module without stopping the old thread: the owner tokens and the
# recent-fire map live on ``sys`` so they survive the re-import; a thread whose token is stale exits.
_TOKENS = sys.__dict__.setdefault("_hermes_plugin_thread_tokens", {})
_recent: dict = sys.__dict__.setdefault("_hermes_goal_heartbeat_recent", {})  # (home, sid) -> injected_at


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
    out["interval_minutes"] = max(MIN_INTERVAL_MINUTES, float(out["interval_minutes"]))
    out["escalate_after"] = max(1, int(out["escalate_after"]))
    return out


def _plugin_still_enabled() -> bool:
    """A disable + reload unloads the plugin without re-running register(): stop on our own."""
    try:
        from hermes_cli.config import load_config_readonly
        enabled = ((load_config_readonly() or {}).get("plugins") or {}).get("enabled") or []
        return PLUGIN_ID in enabled
    except Exception:
        return True  # unreadable config is not a reason to stop


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


def _lease_key(con: sqlite3.Connection, sid: str) -> str:
    """Mirror of SessionDB._session_turn_lease_key: walk compression parents to the conversation root."""
    current, seen = sid, {sid}
    while True:
        row = con.execute("select parent_session_id from sessions where id=?", (current,)).fetchone()
        parent = row[0] if row else None
        if not parent or parent in seen:
            return current
        prow = con.execute("select end_reason from sessions where id=?", (parent,)).fetchone()
        if not prow or prow[0] != "compression":
            return current
        seen.add(parent)
        current = parent


def _turn_running(con: sqlite3.Connection, sid: str, now: float) -> bool:
    """A live turn lease: the session is mid-turn, so an injection would only queue behind it
    (and a long turn persists its rows at the end, so the transcript alone looks idle)."""
    try:
        row = con.execute("select 1 from session_turn_leases where conversation_id=? and expires_at>?",
                          (_lease_key(con, sid), now)).fetchone()
    except sqlite3.OperationalError:  # older schema without leases
        return False
    return row is not None


def _last_activity(con: sqlite3.Connection, sid: str, now: float) -> float:
    """Freshest of the newest message and the session's activity heartbeat (~60 s while running)."""
    last = con.execute("select max(timestamp) from messages where session_id=?", (sid,)).fetchone()[0] or 0.0
    try:
        beat = con.execute("select last_activity_at from sessions where id=?", (sid,)).fetchone()
        beat = float(beat[0]) if beat and beat[0] else 0.0
    except (sqlite3.OperationalError, TypeError, ValueError):
        beat = 0.0
    return max(float(last), beat if beat <= now + 60 else 0.0)  # ignore garbage future stamps


def _is_synthetic_sql() -> str:
    return " or ".join("instr(substr(coalesce(content,''),1,200), ?) > 0" for _ in SYNTHETIC_PREFIXES)


def candidates(db_path: Path, interval_s: float, escalate_after: int, now: float):
    """Yield (session_id, session_key, k, idle_s, goal) for each heartbeat due now; k is 1-based."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    synthetic = _is_synthetic_sql()
    try:
        for key, raw in con.execute("select key, value from state_meta where key like 'goal:%'").fetchall():
            try:
                goal = json.loads(raw)
            except Exception:
                continue
            if goal.get("status") != "active":
                continue
            sid = key[len("goal:"):]
            row = con.execute("select session_key, ended_at from sessions where id=?", (sid,)).fetchone()
            if not row or not row[0] or row[1] is not None or not str(row[0]).startswith("agent:"):
                continue
            last = _last_activity(con, sid, now)
            if not last:
                continue
            idle = now - last
            if idle < interval_s or _turn_running(con, sid, now):
                continue
            real = con.execute(
                f"select coalesce(max(id), 0) from messages where session_id=? and role='user' "
                f"and not ({synthetic})", (sid, *SYNTHETIC_PREFIXES)).fetchone()[0]
            beats = con.execute(
                "select count(*) from messages where session_id=? and role='user' and id>? "
                "and instr(substr(coalesce(content,''),1,200), ?) > 0", (sid, real, MARKER)).fetchone()[0]
            if beats >= escalate_after:
                continue  # already escalated; wait for something real to happen
            yield sid, row[0], beats + 1, idle, goal
    finally:
        con.close()


def _parked(goal: dict) -> bool:
    return bool(goal.get("waiting_on_session") or goal.get("waiting_on_pid") or float(goal.get("waiting_until") or 0))


def render(k: int, n: int, idle_s: float, goal: dict) -> str:
    head = f"{MARKER}{k}/{n} — no session activity for {int(idle_s // 60)}m; goal waiting on {_target(goal)}]"
    body = f"{head}\nGoal: {goal.get('goal', '')}\n"
    if k >= n:
        return (body + f"This is check {k} with no progress since the last real event. Do not reply [SILENT]. Send "
                "the user one short message: what the goal is waiting on, why it has not moved, and the one thing "
                "you need from them (or your default if they do nothing). Heartbeats stop until something new "
                "happens here.")
    if not _parked(goal):
        # Not parked: a silent reply would be judged as not-waiting and continue the loop, so there is
        # no [SILENT] option here. The goal is active and nothing is driving it.
        return (body + "The goal is active but nothing is set to resume it. Take the next concrete step now. If "
                "you are genuinely waiting on something, start a waker for it (a background process with notify "
                "that exits when the condition changes) and say in one line what you are waiting on.")
    return (body + "Check that what you are waiting on is still progressing and is still the right thing to "
            "wait on: the process is alive and its output is moving, the CI run/deploy/agent is actually "
            "running, nothing is parked on an approval, and the condition can still happen. "
            "If something finished, broke, or changed, act on it now. If it is healthy, make sure something "
            "will wake you (a background process with notify that exits when the condition changes), then "
            "reply with exactly [SILENT] and nothing else.")


def _tick(ctx, home: Path) -> None:
    cfg = _settings()
    if not cfg["enabled"]:
        return
    interval_s = cfg["interval_minutes"] * 60
    now = time.time()
    for sid, key, k, idle, goal in candidates(home / "state.db", interval_s, cfg["escalate_after"], now):
        # Covers an injection not yet visible in the session; once its turn starts the session holds a
        # lease and then has fresh rows. One that never starts (the gateway dropped the dispatch) is
        # retried after RETRY_SECONDS; candidates() skips sessions mid-turn, so a heartbeat is never
        # queued behind a running turn in the first place.
        if now - _recent.get((str(home), sid), 0) < RETRY_SECONDS:
            continue
        ok = False
        try:
            ok = bool(ctx.inject_message(render(k, cfg["escalate_after"], idle, goal), role="user", session_key=key))
        except Exception:
            logger.warning("goal-heartbeat: inject failed for %s", key, exc_info=True)
        if ok:
            _recent[(str(home), sid)] = now
            logger.info("goal-heartbeat: fired %d/%d -> %s (idle %dm)", k, cfg["escalate_after"], sid, idle // 60)


def _loop(ctx, token_key: str, token, home: Path) -> None:
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(home)  # this thread's config reads and injection checks use its profile
    while _TOKENS.get(token_key) is token:
        try:
            if not _plugin_still_enabled():
                logger.info("goal-heartbeat: disabled for %s; thread exiting", home)
                return
            if _in_gateway_process():
                _tick(ctx, home)
        except Exception:
            logger.warning("goal-heartbeat: tick error", exc_info=True)
        time.sleep(POLL_SECONDS)


def register(ctx):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()  # the profile this load is scoped to (multiplexed gateways load once per profile)
    token_key, token = f"{PLUGIN_ID}:{home}", object()
    _TOKENS[token_key] = token  # retires any thread from a previous load of this profile
    threading.Thread(target=_loop, args=(ctx, token_key, token, home), name=PLUGIN_ID, daemon=True).start()


if __name__ == "__main__" and "--dry-run" in sys.argv:
    args = sys.argv
    interval = float(args[args.index("--interval") + 1]) if "--interval" in args else DEFAULTS["interval_minutes"]
    n = int(args[args.index("--escalate") + 1]) if "--escalate" in args else DEFAULTS["escalate_after"]
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    found = False
    for sid, key, k, idle, goal in candidates(home / "state.db", interval * 60, n, time.time()):
        found = True
        print(f"WOULD FIRE {k}/{n} -> {sid} ({key}) idle={int(idle // 60)}m")
        print("  " + render(k, n, idle, goal).replace("\n", "\n  "))
    if not found:
        print(f"nothing due (interval={interval}m, escalate_after={n})")
