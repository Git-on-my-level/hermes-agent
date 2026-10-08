"""session-inject: deliver queued user turns into existing gateway sessions.

``hermes inject`` (see ``cli.py``) writes one JSON file per request into
``$HERMES_HOME/state/inject-spool/`` (``{"session_key": ..., "content": ...}``). Inside the gateway
process only, a daemon thread drains that directory through the supported plugin API
(``ctx.inject_message``), which routes the turn through the session's live adapter like a user
message. Accepted requests move to ``done/``; requests the gateway keeps refusing (unknown session,
gateway draining) move to ``failed/`` after ~10 min.

Requires ``plugins.enabled`` to list this plugin and
``plugins.entries.session-inject.allow_gateway_injection: true``.
The spool directory is mode 700: anyone who can write there can start turns.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
MAX_ATTEMPTS = 120  # ~10 minutes of refusals before giving up on a request
# A plugin reload re-executes this module without stopping the old thread. The current owner token
# lives on ``sys`` (survives the re-import); a thread whose token is no longer current exits.
_TOKENS = sys.__dict__.setdefault("_hermes_plugin_thread_tokens", {})
_TOKEN_KEY = "session-inject"


def spool_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "state" / "inject-spool"


def _in_gateway_process() -> bool:
    try:
        from hermes_cli import plugins as host
        return getattr(host, "_published_gateway_message_injector", None) is not None
    except Exception:
        return False


def _move(path: Path, sub: str, record: dict) -> None:
    dest = path.parent / sub
    dest.mkdir(mode=0o700, exist_ok=True)
    (dest / path.with_suffix(".json").name).write_text(json.dumps(record, indent=1), encoding="utf-8")
    path.unlink(missing_ok=True)


def drain_once(ctx, spool: Path | None = None) -> None:
    spool = spool or spool_dir()
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
            _move(path, "failed", {"file": path.name, "error": str(exc)})
            continue
        ok = False
        try:
            ok = bool(ctx.inject_message(content, role="user", session_key=key))
        except Exception:
            logger.warning("session-inject: inject failed for %s", key, exc_info=True)
        record["attempts"] = int(record.get("attempts", 0)) + 1
        if ok:
            record["delivered_at"] = time.time()
            logger.info("session-inject: delivered %s -> %s", queued.name, key)
            _move(path, "done", record)
        elif record["attempts"] >= MAX_ATTEMPTS:
            logger.warning("session-inject: giving up on %s -> %s", queued.name, key)
            _move(path, "failed", record)
        else:
            path.write_text(json.dumps(record), encoding="utf-8")
            path.rename(queued)  # release for the next poll


def _loop(ctx, token) -> None:
    while _TOKENS.get(_TOKEN_KEY) is token:
        try:
            if _in_gateway_process() and spool_dir().is_dir():
                drain_once(ctx)
        except Exception:
            logger.warning("session-inject: drain loop error", exc_info=True)
        time.sleep(POLL_SECONDS)


def register(ctx):
    from .cli import handle, setup
    ctx.register_cli_command("inject", "Queue a user turn into an existing gateway session",
                             setup, handle, description=(__doc__ or "").split("\n\n")[0])
    token = object()
    _TOKENS[_TOKEN_KEY] = token  # retires any thread from a previous load
    threading.Thread(target=_loop, args=(ctx, token), name="session-inject", daemon=True).start()
