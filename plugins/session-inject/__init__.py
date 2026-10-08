"""session-inject: deliver queued user turns into existing gateway sessions.

``hermes inject`` (see ``cli.py``) writes one JSON file per request into
``$HERMES_HOME/state/inject-spool/`` (``{"session_key": ..., "content": ...}``). Inside the gateway
process only, a daemon thread drains that directory through the supported plugin API
(``ctx.inject_message``), which routes the turn through the session's live adapter like a user
message.

``inject_message`` returning True means the gateway scheduled the dispatch, not that the session
took it (an unknown route or failed authorization is only logged). So a dispatched request moves
to ``sent/`` and is confirmed against the session's own history: ``done/`` once the message is
persisted as a user turn, ``failed/`` if it never shows up within ``CONFIRM_SECONDS``. Requests the
gateway refuses outright are retried every poll and move to ``failed/`` after ~10 min.

Requires ``plugins.enabled`` to list this plugin and
``plugins.entries.session-inject.allow_gateway_injection: true``.
The spool directory is mode 700: anyone who can write there can start turns.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ID = "session-inject"
POLL_SECONDS = 5.0
MAX_ATTEMPTS = 120  # ~10 minutes of refusals before giving up on a request
CONFIRM_SECONDS = 1800.0  # a dispatched turn can wait behind a long running turn
STALE_INFLIGHT_SECONDS = 60.0
# A plugin reload re-executes this module without stopping the old thread. The current owner token
# lives on ``sys`` (survives the re-import); a thread whose token is no longer current exits.
_TOKENS = sys.__dict__.setdefault("_hermes_plugin_thread_tokens", {})


def spool_dir(home: Path | None = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home) / "state" / "inject-spool"


def _in_gateway_process() -> bool:
    try:
        from hermes_cli import plugins as host
        return getattr(host, "_published_gateway_message_injector", None) is not None
    except Exception:
        return False


def _plugin_still_enabled() -> bool:
    """A disable + reload unloads the plugin without re-running register(): stop on our own."""
    try:
        from hermes_cli.config import load_config_readonly
        enabled = ((load_config_readonly() or {}).get("plugins") or {}).get("enabled") or []
        return PLUGIN_ID in enabled
    except Exception:
        return True


def _move(path: Path, spool: Path, sub: str, record: dict) -> None:
    dest = spool / sub
    dest.mkdir(mode=0o700, exist_ok=True)
    (dest / path.with_suffix(".json").name).write_text(json.dumps(record, indent=1), encoding="utf-8")
    path.unlink(missing_ok=True)


def recover_stale_inflight(spool: Path, now: float | None = None) -> int:
    """Return claims left by a process that died mid-dispatch to the queue."""
    now = time.time() if now is None else now
    n = 0
    for path in spool.glob("*.inflight"):
        try:
            if now - path.stat().st_mtime >= STALE_INFLIGHT_SECONDS:
                path.rename(path.with_suffix(".json"))
                n += 1
        except OSError:
            continue
    return n


def _snippet(content: str) -> str:
    """The part of the text the gateway persists verbatim: it strips leading timestamp prefixes
    (and stores their time as the row time) and may prepend a sender label."""
    text = content
    try:
        from gateway.message_timestamps import strip_leading_message_timestamps
        text = strip_leading_message_timestamps(content)[0]
    except Exception:
        pass
    return text.strip()[:200]


def _max_message_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return int(con.execute("select coalesce(max(id), 0) from messages").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def _observed(db_path: Path, session_key: str, content: str, after_id: int) -> bool:
    """True once the injected text is persisted as a user turn in that session (any session id
    the key has had, so compression rotation is covered). Bounded by row id, not timestamp: the
    gateway may stamp a row with a time embedded in the text."""
    snippet = _snippet(content)
    if not snippet or not db_path.exists():
        return False
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            "select 1 from messages m join sessions s on s.id = m.session_id "
            "where s.session_key = ? and m.role = 'user' and m.id > ? "
            "and instr(m.content, ?) > 0 limit 1", (session_key, after_id, snippet)).fetchone()
        return row is not None
    finally:
        con.close()


def confirm_sent(spool: Path, db_path: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    sent = spool / "sent"
    if not sent.is_dir():
        return
    for path in sorted(sent.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            sent_at = float(record["sent_at"])
            if _observed(db_path, record["session_key"], record["content"], int(record.get("after_id", 0))):
                record["confirmed_at"] = now
                _move(path, spool, "done", record)
                logger.info("session-inject: confirmed %s -> %s", path.name, record["session_key"])
            elif now - sent_at > CONFIRM_SECONDS:
                record["error"] = f"dispatched but never observed in the session within {int(CONFIRM_SECONDS)}s"
                _move(path, spool, "failed", record)
                logger.warning("session-inject: %s never reached %s", path.name, record["session_key"])
        except Exception:
            logger.warning("session-inject: confirm failed for %s", path.name, exc_info=True)


def drain_once(ctx, spool: Path | None = None, db_path: Path | None = None) -> None:
    spool = spool or spool_dir()
    db_path = db_path or spool.parent.parent / "state.db"
    for queued in sorted(spool.glob("*.json")):
        path = queued.with_suffix(".inflight")
        try:
            queued.rename(path)  # claim: only one thread (old or new load) can win the rename
        except OSError:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            key, content = record["session_key"], record["content"]
        except Exception as exc:
            logger.warning("session-inject: unreadable request %s: %s", path.name, exc)
            _move(path, spool, "failed", {"file": path.name, "error": str(exc)})
            continue
        ok = False
        after_id = _max_message_id(db_path)  # before dispatch, so the persisted row's id is larger
        try:
            ok = bool(ctx.inject_message(content, role="user", session_key=key))
        except Exception:
            logger.warning("session-inject: inject failed for %s", key, exc_info=True)
        record["attempts"] = int(record.get("attempts", 0)) + 1
        if ok:
            record["sent_at"], record["after_id"] = time.time(), after_id
            logger.info("session-inject: dispatched %s -> %s", queued.name, key)
            _move(path, spool, "sent", record)
        elif record["attempts"] >= MAX_ATTEMPTS:
            logger.warning("session-inject: giving up on %s -> %s", queued.name, key)
            _move(path, spool, "failed", record)
        else:
            path.write_text(json.dumps(record), encoding="utf-8")
            path.rename(queued)  # release for the next poll


def _loop(ctx, token_key: str, token, home: Path) -> None:
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(home)  # this thread's config reads and injection checks use its profile
    spool = spool_dir(home)
    while _TOKENS.get(token_key) is token:
        try:
            if not _plugin_still_enabled():
                logger.info("session-inject: disabled for %s; thread exiting", home)
                return
            if _in_gateway_process() and spool.is_dir():
                recover_stale_inflight(spool)
                drain_once(ctx, spool, home / "state.db")
                confirm_sent(spool, home / "state.db")
        except Exception:
            logger.warning("session-inject: drain loop error", exc_info=True)
        time.sleep(POLL_SECONDS)


def register(ctx):
    from hermes_constants import get_hermes_home

    from .cli import handle, setup
    ctx.register_cli_command("inject", "Queue a user turn into an existing gateway session",
                             setup, handle, description=(__doc__ or "").split("\n\n")[0])
    home = get_hermes_home()  # the profile this load is scoped to (multiplexed gateways load once per profile)
    token_key, token = f"{PLUGIN_ID}:{home}", object()
    _TOKENS[token_key] = token  # retires any thread from a previous load of this profile
    threading.Thread(target=_loop, args=(ctx, token_key, token, home), name=PLUGIN_ID, daemon=True).start()
