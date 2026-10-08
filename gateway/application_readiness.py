"""Hostctl application-readiness receipt.

One regular file, ``<HERMES_HOME>/state/application-readiness.json``, in the
``hostctl.application_readiness/v1`` shape. It is built from the gateway process
identity and the event-loop timestamp the caller captured on the loop. A thread
that only performs the write cannot refresh ``signals[event_loop]``.

The read-only health probe does not call :func:`publish_application_readiness`.
Missing kernel start time writes nothing: a guessed timestamp would look like a
live process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APPLICATION_ID = "hermes-gateway"
SCHEMA_VERSION = "1"
_PHASES = frozenset({"starting", "maintenance", "ready", "degraded", "stopped"})
_MAX_BYTES = 65536
_RECEIPT_NAME = "application-readiness.json"


def receipt_path(home: Path | None = None) -> Path:
    from hermes_constants import get_process_hermes_home

    base = Path(home) if home is not None else get_process_hermes_home()
    return base / "state" / _RECEIPT_NAME


def publish_application_readiness(
    *,
    phase: str,
    event_loop_at: datetime | None = None,
    progress: dict[str, Any] | None = None,
    exit_reason: str | None = None,
    home: Path | None = None,
    pid: int | None = None,
) -> Path | None:
    """Atomically publish one receipt. Never raises. Returns the path, or None when withheld."""
    try:
        return _publish(
            phase=phase,
            event_loop_at=event_loop_at,
            progress=progress,
            exit_reason=exit_reason,
            home=home,
            pid=pid,
        )
    except Exception:
        return None


def kernel_started_at(pid: int) -> datetime | None:
    """Kernel start of ``pid``, using the same observation hostctl uses.

    Linux: ``/proc/<pid>/stat`` start ticks plus ``btime``. macOS: ``ps -o lstart``
    at second resolution in the local zone. Other platforms return None.
    """
    if pid <= 1:
        return None
    if sys.platform == "linux":
        return _linux_started_at(pid)
    if sys.platform == "darwin":
        return _darwin_started_at(pid)
    return None


def parse_linux_start(stat_text: str, btime_seconds: int, hz: int) -> datetime | None:
    """Wall time of a Linux ``starttime`` field. Integer division matches hostctl."""
    if hz <= 0:
        return None
    ticks = _linux_start_ticks(stat_text)
    if ticks is None:
        return None
    delta_ns = ticks * 1_000_000_000 // hz
    boot = datetime.fromtimestamp(btime_seconds, timezone.utc)
    return boot + timedelta(microseconds=delta_ns // 1000)


def parse_darwin_lstart(output: str) -> datetime | None:
    """Parse ``ps -o lstart`` as UTC.

    The process is invoked with ``TZ=UTC``, and the timestamp is read as UTC.
    Parsing it in the caller's local zone disagrees with a hostctl reader that
    does the same, by the offset between those zones.
    """
    fields = output.split()
    if len(fields) < 6 or fields[0].startswith("Z"):
        return None
    try:
        naive = datetime.strptime(" ".join(fields[1:6]), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc)


def project_serving_phase(home: Path, pid: int) -> str:
    """Phase a looping gateway may claim from files it already writes.

    Blocking maintenance overrides the loop only when the record names this
    process's kernel start. Draining is not ready: the gateway is no longer
    accepting work. ``stale`` is an observer verdict, not a phase in the receipt.
    """
    maintenance = _read_json(home / "state" / "gateway-maintenance.json")
    if isinstance(maintenance, dict) and _maintenance_names_this_process(maintenance, pid):
        return "maintenance"
    state = _read_json(home / "gateway_state.json")
    claimed = state.get("gateway_state") if isinstance(state, dict) else None
    if claimed in {"degraded", "draining"}:
        return "degraded"
    if claimed == "running":
        return "ready"
    return "starting"


def _maintenance_names_this_process(maintenance: dict, pid: int) -> bool:
    if maintenance.get("blocking") is not True or _coerce_pid(maintenance.get("pid")) != pid:
        return False
    recorded = maintenance.get("pid_started_at")
    if not isinstance(recorded, str):
        return False
    observed = kernel_started_at(pid)
    if observed is None:
        return False
    try:
        claimed = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
    except ValueError:
        return False
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=timezone.utc)
    return abs((observed - claimed.astimezone(timezone.utc)).total_seconds()) <= 2.0


def _publish(
    *,
    phase: str,
    event_loop_at: datetime | None,
    progress: dict[str, Any] | None,
    exit_reason: str | None,
    home: Path | None,
    pid: int | None,
) -> Path | None:
    if phase not in _PHASES:
        return None
    process = os.getpid() if pid is None else int(pid)
    if process <= 1:
        return None
    # A ready receipt requires a timestamp captured while the loop was scheduling.
    # The launchd wrapper and a blocked loop do not have one.
    if phase == "ready" and event_loop_at is None:
        phase = "starting"
    if phase == "stopped":
        event_loop_at = None
    started = kernel_started_at(process)
    if started is None:
        return None
    path = receipt_path(home)
    base = path.parent.parent
    now = datetime.now(timezone.utc)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "application_id": APPLICATION_ID,
        "phase": phase,
        "pid": process,
        "pid_started_at": _rfc3339_z(started),
        "updated_at": _rfc3339_z(now),
    }
    revision = _running_revision(base, process)
    if revision is not None:
        document["revision"] = revision
    projected_progress = _progress(progress)
    if projected_progress is not None:
        document["progress"] = projected_progress
    reason = _reason(exit_reason)
    if reason is not None:
        document["exit_reason"] = reason
    if event_loop_at is not None:
        document["signals"] = [{
            "name": "event_loop",
            "updated_at": _rfc3339_z(event_loop_at),
        }]
    document["references"] = _references(base)
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_BYTES:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return path


def _running_revision(home: Path, pid: int) -> str | None:
    state = _read_json(home / "gateway_state.json")
    if not isinstance(state, dict) or _coerce_pid(state.get("pid")) != pid:
        return None
    sha = state.get("code_sha")
    if isinstance(sha, str) and _token(sha):
        return sha
    return None


def _progress(progress: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(progress, dict):
        return None
    summary = progress.get("summary")
    if not isinstance(summary, str):
        return None
    summary = _plain(summary, 256)
    if summary is None:
        return None
    projected = {"summary": summary}
    deadline = progress.get("deadline_at")
    if isinstance(deadline, str):
        normalized = _utc_timestamp(deadline)
        if normalized is not None:
            projected["deadline_at"] = normalized
    return projected


def _references(home: Path) -> list[dict[str, str]]:
    rows = (
        ("heartbeat", home / "state" / "gateway.heartbeat"),
        ("state", home / "gateway_state.json"),
        ("log", home / "logs" / "gateway.log"),
    )
    return [{"role": role, "path": str(path)} for role, path in rows]


def _reason(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return _plain(value, 256)


def _plain(value: str, limit: int) -> str | None:
    text = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    if not text:
        return None
    return text[:limit]


def _token(value: str) -> bool:
    if not value or len(value) > 128 or value != value.strip():
        return False
    return all(ch >= " " and ch != "\x7f" for ch in value)


def _utc_timestamp(value: str) -> str | None:
    text = value.strip()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    if not text.endswith("Z"):
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def rfc3339_utc(value: datetime) -> str:
    """UTC RFC3339 with a ``Z`` suffix. Hostctl rejects numeric offsets."""
    return _rfc3339_z(value)


def _rfc3339_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    fraction = f".{utc.microsecond:06d}" if utc.microsecond else ""
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def _linux_started_at(pid: int) -> datetime | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8-sig")
        hz = int(os.sysconf("SC_CLK_TCK"))
        btime = _linux_btime()
    except (OSError, ValueError):
        return None
    if btime is None:
        return None
    return parse_linux_start(stat_text, btime, hz)


def _linux_btime() -> int | None:
    try:
        text = Path("/proc/stat").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _linux_start_ticks(stat_text: str) -> int | None:
    end = stat_text.rfind(")")
    if end < 0 or end + 2 >= len(stat_text):
        return None
    fields = stat_text[end + 2:].split()
    if len(fields) < 20 or fields[0].startswith("Z"):
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _darwin_started_at(pid: int) -> datetime | None:
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "state=,lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return parse_darwin_lstart(completed.stdout)


def _read_json(path: Path) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data


def _coerce_pid(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 1 else None
