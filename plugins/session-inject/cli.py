"""``hermes inject``: queue a user turn for the gateway's session-inject drain."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{_home() / 'state.db'}?mode=ro", uri=True)


def resolve_key(ident: str) -> str:
    if ident.startswith("agent:"):
        return ident
    row = _db().execute("select session_key from sessions where id=?", (ident,)).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"no session_key for session id {ident}")
    return row[0]


def queue(session_key: str, content: str) -> Path:
    spool = _home() / "state" / "inject-spool"
    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = spool / f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session_key": session_key, "content": content, "queued_at": time.time()}))
    tmp.rename(path)
    return path


def list_stalled(hours: float) -> None:
    """Open gateway sessions active in the window, with the role of their last row."""
    since = time.time() - hours * 3600
    con = _db()
    q = """select s.id, s.title, max(m.timestamp) from sessions s
           join messages m on m.session_id=s.id
           where s.session_key like 'agent:%' and s.ended_at is null and m.timestamp > ?
           group by s.id order by max(m.timestamp) desc"""
    for sid, title, last in con.execute(q, (since,)).fetchall():
        role, content = con.execute(
            "select role, coalesce(content,'') from messages where session_id=? order by id desc limit 1",
            (sid,)).fetchone()
        tail = " ".join(content.split())[:140]
        print(f"{sid}\t{time.strftime('%H:%M', time.localtime(last))}\t{role}\t{(title or '')[:40]}\t{tail}")


def setup(parser) -> None:
    parser.add_argument("session", nargs="?", help="session id or agent:... session key")
    parser.add_argument("message", nargs="?", help="text delivered as a user turn")
    parser.add_argument("--list-stalled", action="store_true", help="list recent gateway sessions")
    parser.add_argument("--hours", type=float, default=6)


def handle(args) -> int:
    if args.list_stalled:
        list_stalled(args.hours)
        return 0
    if not args.session or not args.message:
        raise SystemExit("usage: hermes inject <session_id|session_key> \"message\"")
    path = queue(resolve_key(args.session), args.message)
    print(f"queued {path.name}; the gateway delivers it within ~5 s (see inject-spool/done)")
    return 0
