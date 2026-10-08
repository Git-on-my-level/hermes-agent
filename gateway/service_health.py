"""Read-only gateway health that stays usable when the application cannot start.

Stdlib plus ``hermes_constants`` only. The command is dispatched from
``hermes_bootstrap`` before dependency sync, plugin import, or credential load.
It reports the files the running gateway already writes (PID, ``gateway_state.json``,
loop heartbeat, loop-tick socket) and does not create a second status store.

The gateway process publishes ``state/application-readiness.json`` for hostctl.
This probe only reads that file. It does not write a receipt, import plugins, or
read credentials.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_process_hermes_home

_SCHEMA = "hermes.service_health.v1"
_HEARTBEAT_FRESH_S = 90.0
_LOOP_TICK_TIMEOUT_S = 0.2
_CONNECTED = {"connected", "running", "ok"}
_RUNNING_STATES = {"running", "degraded", "starting", "draining"}
# Top-level flags that take a value, so `hermes -p name gateway health` still matches.
# Kept local: importing the CLI parser pulls the rest of the application.
_VALUE_FLAGS = frozenset({
    "-p", "--profile", "-m", "--model", "--provider", "--reasoning",
    "-z", "--oneshot", "-t", "--toolsets", "-r", "--resume", "-s", "--skills",
    "--usage-file", "--output-format", "--in",
})


def command_tokens(argv: list[str]) -> list[str]:
    """Positional argv, skipping a fixed set of value flags. Does not import the CLI parser."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1:]
        if token.startswith("-"):
            index += 2 if "=" not in token and token in _VALUE_FLAGS and index + 1 < len(argv) else 1
            continue
        return argv[index:]
    return []


def maybe_run_readonly_health(argv: list[str]) -> int | None:
    """Run the probe when argv is ``gateway health``. ``None`` leaves bootstrap to continue."""
    tokens = command_tokens(argv)
    if tokens[:2] != ["gateway", "health"]:
        return None
    return main(tokens[2:])


def main(argv: list[str] | None = None) -> int:
    """Print one JSON document on stdout. Exit 0 when the probe itself succeeded.

    The gateway's phase is a field. A dead or stale gateway is still a successful read:
    callers that treat a non-zero exit as 'probe failed' would otherwise hide the evidence.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(
            "Usage: hermes gateway health\n"
            "Read-only gateway phase, identity, heartbeat, and update progress.\n"
            "Does not sync dependencies, load plugins, or read credentials.",
            file=sys.stdout,
        )
        return 0
    source = Path(__file__).resolve().parents[1]
    document = collect_service_health(source_root=source)
    json.dump(document, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def collect_service_health(
    home: Path | None = None,
    *,
    source_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Observe one Hermes home. Reads files and one short loop-tick connect; writes nothing."""
    home = Path(home) if home is not None else get_process_hermes_home()
    observed = now or datetime.now(timezone.utc)
    state = _read_json(home / "gateway_state.json") or {}
    pid_record = _read_json(home / "gateway.pid") or {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(pid_record, dict):
        pid_record = {}
    heartbeat = _read_json(home / "state" / "gateway.heartbeat") or {}
    maintenance = _read_json(home / "state" / "gateway-maintenance.json") or {}
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    if not isinstance(maintenance, dict):
        maintenance = {}

    pid = _coerce_pid(state.get("pid")) or _coerce_pid(pid_record.get("pid"))
    recorded_start = state.get("start_time")
    if recorded_start is None:
        recorded_start = pid_record.get("start_time")
    alive = bool(pid) and _pid_alive(pid)
    current_start = _start_fingerprint(pid) if alive and pid else None
    identity = _identity(recorded_start, current_start, alive)
    parent_pid = _parent_pid(pid) if alive and pid else None
    heartbeat_age = _age_seconds(heartbeat.get("updated_at"), observed)
    state_age = _age_seconds(state.get("updated_at"), observed)
    loop_tick = _probe_loop_tick(home, pid) if alive and pid else "unavailable"
    platforms = _platforms(state.get("platforms"), observed)
    update = _update_view(home, source_root, maintenance, observed)
    phase = _phase(
        state=state,
        alive=alive,
        identity=identity,
        heartbeat_age=heartbeat_age,
        loop_tick=loop_tick,
        platforms=platforms,
        maintenance=maintenance,
    )
    gateway_state = state.get("gateway_state") if isinstance(state.get("gateway_state"), str) else None
    document = {
        "schema": _SCHEMA,
        "observed_at": observed.isoformat(),
        "phase": phase,
        "healthy": phase == "ready",
        "gateway_state": gateway_state,
        "exit_reason": state.get("exit_reason") if isinstance(state.get("exit_reason"), str) else None,
        "identity": {
            "pid": pid,
            "parent_pid": parent_pid,
            "start_time": recorded_start if isinstance(recorded_start, (int, float)) and not isinstance(recorded_start, bool) else None,
            "verified": identity == "match",
            "status": identity,
        },
        "freshness": {
            "heartbeat_age_s": heartbeat_age,
            "heartbeat_fresh": heartbeat_age is not None and heartbeat_age <= _HEARTBEAT_FRESH_S,
            "state_age_s": state_age,
            "loop_tick": loop_tick,
        },
        "revisions": _revisions(state, source_root),
        "platforms": platforms,
        "update": update,
        "logs": {
            "gateway": str(home / "logs" / "gateway.log"),
            "errors": str(home / "logs" / "errors.log"),
            "startup_watchdog": str(home / "logs" / "gateway-startup-watchdog.log"),
        },
        "maintenance": {
            "blocking": bool(maintenance.get("blocking")),
            "phase": maintenance.get("phase") if isinstance(maintenance.get("phase"), str) else None,
            "deadline_at": maintenance.get("deadline_at") if isinstance(maintenance.get("deadline_at"), str) else None,
            "waiting_on": maintenance.get("waiting_on") if isinstance(maintenance.get("waiting_on"), str) else None,
            "reason": maintenance.get("reason") if isinstance(maintenance.get("reason"), str) else None,
            "recovery": maintenance.get("recovery") if isinstance(maintenance.get("recovery"), str) else None,
            "pid": _coerce_pid(maintenance.get("pid")),
        },
    }
    document["application_readiness"] = _read_json(home / "state" / "application-readiness.json")
    return document


def _phase(
    *,
    state: dict[str, Any],
    alive: bool,
    identity: str,
    heartbeat_age: float | None,
    loop_tick: str,
    platforms: list[dict[str, Any]],
    maintenance: dict[str, Any],
) -> str:
    maintenance_pid = _coerce_pid(maintenance.get("pid"))
    if maintenance.get("blocking") and maintenance_pid and _pid_alive(maintenance_pid):
        return "maintenance"
    claimed = state.get("gateway_state") if isinstance(state.get("gateway_state"), str) else ""
    if not alive:
        return "stale" if claimed in _RUNNING_STATES else "stopped"
    if identity == "mismatch":
        return "stale"
    heartbeat_fresh = heartbeat_age is not None and heartbeat_age <= _HEARTBEAT_FRESH_S
    loop_live = loop_tick == "answered"
    connected = [row for row in platforms if row.get("connected")]
    disconnected = bool(platforms) and not connected
    if claimed in {"starting"} or (claimed not in {"running", "degraded", "draining"} and not heartbeat_fresh):
        return "starting"
    if identity != "match":
        return "degraded"
    if claimed == "degraded" or disconnected or (not heartbeat_fresh and not loop_live):
        return "degraded"
    if claimed in {"running", "draining"} and (heartbeat_fresh or loop_live):
        return "ready"
    return "starting"


def _identity(recorded: Any, current: int | None, alive: bool) -> str:
    if not alive:
        return "dead"
    if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
        return "unverified"
    if current is None:
        return "unverified"
    if int(recorded) == int(current):
        return "match"
    return "mismatch"


def _platforms(raw: Any, now: datetime) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    rows = []
    for name, payload in raw.items():
        if not isinstance(name, str) or not isinstance(payload, dict):
            continue
        state = payload.get("state") if isinstance(payload.get("state"), str) else None
        updated = payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None
        rows.append({
            "name": name,
            "state": state,
            "connected": (state or "").lower() in _CONNECTED,
            "updated_at": updated,
            "age_s": _age_seconds(updated, now),
            "error_code": payload.get("error_code") if isinstance(payload.get("error_code"), str) else None,
        })
    return rows


def _revisions(state: dict[str, Any], source_root: Path | None) -> dict[str, Any]:
    running = state.get("code_sha") if isinstance(state.get("code_sha"), str) and state.get("code_sha") else None
    installed = None
    if source_root is not None:
        stamp = _read_json(Path(source_root) / "install-stamp.json") or {}
        if isinstance(stamp, dict) and isinstance(stamp.get("commit"), str):
            installed = stamp["commit"]
    return {
        "running": running,
        "installed": installed,
        "match": bool(running and installed and running == installed),
    }


def _update_view(
    home: Path,
    source_root: Path | None,
    maintenance: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    marker = _read_update_marker(home)
    owed = False
    if source_root is not None:
        owed = _completion_pending(Path(source_root))
    recovery = maintenance.get("recovery") if isinstance(maintenance.get("recovery"), str) else None
    if owed and not recovery:
        recovery = "run `hermes update` from a shell to finish it"
    return {
        "owed": owed,
        "phase": maintenance.get("phase") if isinstance(maintenance.get("phase"), str) else None,
        "blocking": bool(maintenance.get("blocking")),
        "owner_pid": marker.get("pid"),
        "owner_age_s": marker.get("age_s"),
        "owner_alive": marker.get("alive"),
        "recovery": recovery,
        "receipt": _latest_receipt(home, now),
    }


def _completion_pending(source_root: Path) -> bool:
    try:
        from pm.environments import install_state_dir
    except Exception:
        return False
    try:
        return (install_state_dir(source_root) / "source-completion-pending").is_file()
    except OSError:
        return False


def _read_update_marker(home: Path) -> dict[str, Any]:
    """Read the update lock marker without deleting it. Health must not mutate."""
    path = home / ".hermes-update-in-progress"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError):
        return {}
    try:
        started = float(lines[1].strip())
    except (IndexError, ValueError):
        started = None
    age = None if started is None else max(0.0, datetime.now(timezone.utc).timestamp() - started)
    return {"pid": pid, "age_s": age, "alive": _pid_alive(pid)}


def _latest_receipt(home: Path, now: datetime) -> dict[str, Any] | None:
    directory = home / "logs" / "update_receipts"
    try:
        files = [path for path in directory.iterdir() if path.is_file()]
    except OSError:
        return None
    if not files:
        return None
    latest = max(files, key=lambda path: path.stat().st_mtime)
    try:
        modified = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return {"name": latest.name}
    summary: dict[str, Any] = {
        "name": latest.name,
        "age_s": round((now - modified).total_seconds(), 3),
    }
    payload = _read_json(latest)
    if isinstance(payload, dict):
        for key in ("exit_code", "stop_reason", "phase"):
            value = payload.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 200:
                summary[key] = value
    return summary


def _probe_loop_tick(home: Path, pid: int) -> str:
    if sys.platform == "win32":
        return "unavailable"
    path = home / "state" / f"gateway.loop-tick.{pid}.sock"
    if not path.exists():
        return "unavailable"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_LOOP_TICK_TIMEOUT_S)
            sock.connect(str(path))
            data = sock.recv(1)
    except (OSError, TimeoutError):
        return "unanswered"
    return "answered" if data == b"1" else "unanswered"


def _start_fingerprint(pid: int) -> int | None:
    """Same fingerprint as ``gateway.status._get_process_start_time``.

    Duplicated so this probe does not import the status writer (and the modules
    it pulls in) when the application cannot start. ``/proc`` on Linux; psutil's
    centisecond create time elsewhere, when that package imports.
    """
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass
    try:
        import psutil
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        parent = int(result.stdout.strip())
    except ValueError:
        return None
    return parent or None


def _coerce_pid(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _age_seconds(raw: Any, now: datetime) -> float | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((now - parsed.astimezone(timezone.utc)).total_seconds(), 3)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(raw) > 1_000_000:
        raw = raw[:1_000_000]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
