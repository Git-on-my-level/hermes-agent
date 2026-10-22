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
import math
import os
import socket
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_process_hermes_home

_SCHEMA = "hermes.service_health.v1"
_HEARTBEAT_FRESH_S = 90.0
_FUTURE_SKEW_S = 2.0
_LOOP_TICK_TIMEOUT_S = 0.2
_READ_LIMIT = 65536
_MAX_PID = 2**32
_CONNECTED = {"connected", "running", "ok"}
_RUNNING_STATES = {"running", "degraded", "starting", "draining"}


def _fallback_value_flags() -> frozenset[str]:
    """Canonical top-level value flags, without building the CLI parser."""
    from hermes_cli._parser import (
        PRE_ARGPARSE_INHERITED_FLAGS,
        _OPTIONAL_VALUE_FLAGS_FALLBACK,
        _VALUE_FLAGS_FALLBACK,
    )

    inherited = {flag for flag, takes_value in PRE_ARGPARSE_INHERITED_FLAGS if takes_value}
    return _VALUE_FLAGS_FALLBACK | _OPTIONAL_VALUE_FLAGS_FALLBACK | inherited


def command_tokens(argv: list[str]) -> list[str]:
    """Positional argv using the shared walk and the parser's fallback flag snapshot."""
    from hermes_cli.profile_argv import command_positionals

    return command_positionals(argv, _fallback_value_flags())


def maybe_run_readonly_health(argv: list[str]) -> int | None:
    """Run the probe when argv is ``gateway health``. ``None`` leaves bootstrap to continue."""
    tokens = command_tokens(argv)
    if tokens[:2] != ["gateway", "health"]:
        return None
    return main(argv)


def explicit_profile_name(argv: list[str]) -> str | None:
    """``-p``/``--profile`` before ``--``, skipping values owned by other flags.

    Uses the same walk as ``hermes_cli.main._scan_profile_flag`` and the parser's
    fallback flag snapshot, so this probe does not build the CLI parser. An invalid
    explicit name raises ``_ProfileUnusable`` instead of borrowing another home.
    A flag after a subcommand that is not a profile id is ignored, matching the CLI.
    """
    from hermes_cli._parser import _OPTIONAL_VALUE_FLAGS_FALLBACK, _VALUE_FLAGS_FALLBACK
    from hermes_cli.profile_argv import scan_profile_flag

    found = scan_profile_flag(argv, _VALUE_FLAGS_FALLBACK, _OPTIONAL_VALUE_FLAGS_FALLBACK)
    if found.rejected:
        if found.saw_subcommand or found.option_looking:
            return None
        raise _ProfileUnusable(f"hermes: {found.rejected!r} is not a profile name")
    if found.name is not None and not _profile_id(found.name):
        raise _ProfileUnusable(f"hermes: {found.name!r} is not a profile name")
    return found.name


def _profile_id(name: str) -> bool:
    from hermes_constants import PROFILE_ID_RE

    return PROFILE_ID_RE.fullmatch(name) is not None


class _ProfileUnusable(Exception):
    """The requested profile cannot be observed without creating or borrowing another home."""


def home_for_explicit_profile(name: str | None) -> Path:
    """Home for an explicit profile flag. ``None`` keeps the process home.

    A named profile that is not already live is refused. Resolving it must not
    create the directory or fall through to another profile's files.
    """
    from hermes_constants import PROFILE_ID_RE, get_process_hermes_home, named_profile_is_live

    current = get_process_hermes_home()
    if name is None:
        return current
    if not PROFILE_ID_RE.fullmatch(name):
        raise _ProfileUnusable(f"hermes: {name!r} is not a profile name")
    root = current.parent.parent if current.parent.name == "profiles" else current
    if name == "default":
        return root
    candidate = root / "profiles" / name
    if not named_profile_is_live(candidate):
        raise _ProfileUnusable(
            f"hermes: profile {name!r} is not a live profile; health will not create it or read another home"
        )
    return candidate


def home_for_health_invocation(argv: list[str]) -> Path:
    """Home a ``gateway health`` invocation observes.

    Explicit ``-p``/``--profile`` wins. Otherwise a root ``HERMES_HOME`` follows the
    sticky ``active_profile`` file, with the same supervisor / Desktop SSH / s6
    exceptions as ``hermes_cli.main._apply_profile_override``. A missing file means
    no selection. A saved name that is not a live profile is an error: health does
    not report the default home's readiness in its place. Recovery commands such as
    ``profile use default`` are not this probe, and this function does not apply
    their fallback.
    """
    from hermes_cli.profile_argv import sticky_profile_applies
    from hermes_constants import (
        PROFILE_ID_RE,
        get_default_hermes_root,
        get_process_hermes_home,
        named_profile_is_live,
    )

    name = explicit_profile_name(argv)
    if name is not None:
        return home_for_explicit_profile(name)
    current = get_process_hermes_home()
    if current.parent.name == "profiles" or not sticky_profile_applies(argv):
        return current
    root = get_default_hermes_root()
    text, evidence = _read_bounded(root / "active_profile")
    if evidence == "missing":
        return current
    if evidence != "ok" or text is None:
        raise _ProfileUnusable(
            "hermes: saved active_profile cannot be read; health will not borrow the default home"
        )
    selected = text.strip().casefold()
    if not selected or selected == "default":
        return root
    if PROFILE_ID_RE.fullmatch(selected) is None:
        raise _ProfileUnusable(
            f"hermes: saved profile {selected!r} is not a profile name; "
            "health will not borrow the default home"
        )
    candidate = root / "profiles" / selected
    if not named_profile_is_live(candidate):
        raise _ProfileUnusable(
            f"hermes: saved profile {selected!r} is not a live profile; "
            "health will not borrow the default home"
        )
    return candidate


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
    try:
        home = home_for_health_invocation(args)
    except _ProfileUnusable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    document = collect_service_health(home, source_root=source)
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
    state, state_evidence = _read_record(home / "gateway_state.json")
    pid_record, pid_evidence = _read_record(home / "gateway.pid")
    heartbeat, heartbeat_evidence = _read_record(home / "state" / "gateway.heartbeat")
    maintenance, maintenance_evidence = _read_record(home / "state" / "gateway-maintenance.json")
    evidence = {
        "state": state_evidence,
        "pid": pid_evidence,
        "heartbeat": heartbeat_evidence,
        "maintenance": maintenance_evidence,
    }
    unreadable = any(item in {"malformed", "unavailable", "oversized"} for item in evidence.values())

    pid = _coerce_pid(state.get("pid")) or _coerce_pid(pid_record.get("pid"))
    recorded_start = state.get("start_time")
    if recorded_start is None:
        recorded_start = pid_record.get("start_time")
    liveness = _pid_liveness(pid)
    alive = liveness == "alive"
    current_start = _start_fingerprint(pid) if alive and pid else None
    identity = "unavailable" if liveness == "unavailable" else _identity(recorded_start, current_start, alive)
    parent_pid = _parent_pid(pid) if alive and pid else None
    heartbeat_age, heartbeat_state, heartbeat_owner = _heartbeat_schedule(
        heartbeat, heartbeat_evidence, pid, identity, observed,
    )
    state_age = _age_seconds(state.get("updated_at"), observed)
    loop_tick = _probe_loop_tick(home, pid) if alive and pid else "unavailable"
    platforms = _platforms(state.get("platforms"), observed)
    maintenance_trusted = _maintenance_trusted(maintenance, observed)
    update = _update_view(home, source_root, maintenance, observed)
    phase = _phase(
        state=state,
        alive=alive,
        liveness=liveness,
        identity=identity,
        heartbeat_age=heartbeat_age,
        state_age=state_age,
        loop_tick=loop_tick,
        platforms=platforms,
        maintenance_trusted=maintenance_trusted,
        unreadable=unreadable,
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
            "heartbeat_fresh": heartbeat_state == "fresh",
            "heartbeat_state": heartbeat_state,
            "heartbeat_owner": heartbeat_owner,
            "state_age_s": state_age,
            "state_clock": _clock_state(state_age, state_evidence),
            "loop_tick": loop_tick,
        },
        "evidence": evidence,
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
            "trusted": maintenance_trusted,
            "evidence": maintenance_evidence,
            "phase": maintenance.get("phase") if isinstance(maintenance.get("phase"), str) else None,
            "deadline_at": maintenance.get("deadline_at") if isinstance(maintenance.get("deadline_at"), str) else None,
            "waiting_on": maintenance.get("waiting_on") if isinstance(maintenance.get("waiting_on"), str) else None,
            "reason": maintenance.get("reason") if isinstance(maintenance.get("reason"), str) else None,
            "recovery": maintenance.get("recovery") if isinstance(maintenance.get("recovery"), str) else None,
            "pid": _coerce_pid(maintenance.get("pid")),
        },
    }
    receipt, receipt_evidence = _read_record(home / "state" / "application-readiness.json")
    document["evidence"]["receipt"] = receipt_evidence
    document["application_readiness"] = receipt or None
    if receipt_evidence in {"malformed", "unavailable", "oversized"} and document["phase"] == "ready":
        document["phase"] = "degraded"
        document["healthy"] = False
    return document


def _heartbeat_schedule(
    heartbeat: dict[str, Any],
    evidence: str,
    gateway_pid: int | None,
    identity: str,
    observed: datetime,
) -> tuple[float | None, str, str]:
    """Age of the loop-captured stamp only when this file names the verified gateway.

    ``updated_at`` is the writer thread's clock. A wrong, missing, or non-finite
    heartbeat PID, or a legacy record with no ``event_loop_at``, does not prove
    this gateway is being scheduled.
    """
    if evidence == "missing":
        return None, "unavailable", "missing"
    if evidence in {"malformed", "unavailable", "oversized"}:
        return None, "indeterminate", "unread"
    raw_pid = heartbeat.get("pid")
    heartbeat_pid = _coerce_pid(raw_pid)
    if heartbeat_pid is None:
        return None, "indeterminate", "missing" if raw_pid is None else "non_finite"
    if gateway_pid is None or identity != "match" or heartbeat_pid != gateway_pid:
        return None, "mismatch", "mismatch"
    stamp = heartbeat.get("event_loop_at")
    if not isinstance(stamp, str) or not stamp.strip():
        return None, "indeterminate", "match"
    age = _age_seconds(stamp, observed)
    return age, _clock_state(age, "ok"), "match"


def _phase(
    *,
    state: dict[str, Any],
    alive: bool,
    liveness: str,
    identity: str,
    heartbeat_age: float | None,
    state_age: float | None,
    loop_tick: str,
    platforms: list[dict[str, Any]],
    maintenance_trusted: bool,
    unreadable: bool,
) -> str:
    if maintenance_trusted:
        return "maintenance"
    claimed = state.get("gateway_state") if isinstance(state.get("gateway_state"), str) else ""
    if liveness == "unavailable" or unreadable:
        return "degraded"
    if not alive:
        return "stale" if claimed in _RUNNING_STATES else "stopped"
    if identity == "mismatch":
        return "stale"
    if identity != "match":
        return "degraded"
    if _is_future(heartbeat_age) or _is_future(state_age) or any(_is_future(row.get("age_s")) for row in platforms):
        return "degraded"
    heartbeat_fresh = _is_fresh(heartbeat_age)
    loop_live = loop_tick == "answered"
    connected = [row for row in platforms if row.get("connected") and _timestamp_supports_connection(row.get("age_s"))]
    disconnected = bool(platforms) and not connected
    if claimed == "draining":
        return "degraded"
    if claimed in {"starting"} or (claimed not in {"running", "degraded"} and not heartbeat_fresh):
        return "starting"
    if claimed == "degraded" or disconnected or (not heartbeat_fresh and not loop_live):
        return "degraded"
    if claimed == "running" and (heartbeat_fresh or loop_live):
        return "ready"
    return "starting"


def _identity(recorded: Any, current: int | None, alive: bool) -> str:
    if not alive:
        return "dead"
    recorded_start = _coerce_start(recorded)
    if recorded_start is None or current is None:
        return "unverified"
    if recorded_start == int(current):
        return "match"
    return "mismatch"


def _coerce_start(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_future(age: float | None) -> bool:
    return age is not None and age < -_FUTURE_SKEW_S


def _is_fresh(age: float | None) -> bool:
    return age is not None and -_FUTURE_SKEW_S <= age <= _HEARTBEAT_FRESH_S


def _clock_state(age: float | None, evidence: str) -> str:
    if evidence in {"malformed", "unavailable", "oversized"}:
        return "indeterminate"
    if evidence == "missing" or age is None:
        return "unavailable"
    if _is_future(age):
        return "indeterminate"
    if _is_fresh(age):
        return "fresh"
    return "stale"


def _timestamp_supports_connection(age: float | None) -> bool:
    """A platform stamp in the future, or with no parseable time, is not a live connection."""
    return _is_fresh(age)


def _maintenance_trusted(maintenance: dict[str, Any], now: datetime) -> bool:
    """A blocking marker overrides the gateway only with a live, matching kernel start and a fresh clock."""
    if maintenance.get("blocking") is not True:
        return False
    pid = _coerce_pid(maintenance.get("pid"))
    if pid is None or _pid_liveness(pid) != "alive":
        return False
    recorded = maintenance.get("pid_started_at")
    if not isinstance(recorded, str):
        return False
    from gateway.application_readiness import kernel_started_at

    observed = kernel_started_at(pid)
    claimed = _parse_time(recorded)
    if observed is None or claimed is None:
        return False
    if abs((observed - claimed).total_seconds()) > _FUTURE_SKEW_S:
        return False
    started_age = _age_seconds(maintenance.get("started_at"), now)
    if started_age is None or _is_future(started_age):
        return False
    deadline_age = _age_seconds(maintenance.get("deadline_at"), now)
    if deadline_age is None:
        return started_age <= _HEARTBEAT_FRESH_S
    # deadline_at in the future has a negative age. Past the skew, the marker is stale.
    return deadline_age <= _FUTURE_SKEW_S


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
            "connected": (state or "").lower() in _CONNECTED and _timestamp_supports_connection(_age_seconds(updated, now)),
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
    text, evidence = _read_bounded(home / ".hermes-update-in-progress")
    if evidence != "ok" or text is None:
        return {}
    lines = text.splitlines()
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError, OverflowError):
        return {}
    pid = _coerce_pid(pid)
    if pid is None:
        return {}
    try:
        started = float(lines[1].strip())
    except (IndexError, ValueError, OverflowError):
        started = None
    if started is None or not math.isfinite(started):
        age = None
    else:
        age = max(0.0, datetime.now(timezone.utc).timestamp() - started)
    return {"pid": pid, "age_s": age, "alive": _pid_alive(pid)}


def _latest_receipt(home: Path, now: datetime) -> dict[str, Any] | None:
    directory = home / "logs" / "update_receipts"
    try:
        names = list(directory.iterdir())
    except FileNotFoundError:
        return None
    except OSError:
        return {"state": "unavailable"}
    latest: Path | None = None
    latest_mtime: float | None = None
    for path in names:
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        if latest_mtime is None or info.st_mtime >= latest_mtime:
            latest, latest_mtime = path, info.st_mtime
    if latest is None or latest_mtime is None:
        return None
    try:
        modified = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return {"name": latest.name, "state": "indeterminate"}
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
        return int(stat_path.read_text(encoding="utf-8-sig").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass
    try:
        import psutil
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def _pid_liveness(pid: int | None) -> str:
    """``alive``, ``dead``, or ``unavailable``. Never uses ``os.kill(pid, 0)`` (destructive on Windows)."""
    if pid is None or pid <= 1 or pid >= _MAX_PID:
        return "dead"
    if sys.platform.startswith("linux"):
        proc = Path(f"/proc/{pid}")
        try:
            return "alive" if proc.is_dir() else "dead"
        except OSError:
            return "unavailable"
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "pid="],
                capture_output=True, text=True, encoding="utf-8", timeout=1, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return "alive" if completed.returncode == 0 and completed.stdout.strip() else "dead"
    try:
        import psutil
        return "alive" if psutil.pid_exists(pid) else "dead"
    except Exception:
        return "unavailable"


def _pid_alive(pid: int | None) -> bool:
    return _pid_liveness(pid) == "alive"


def _parent_pid(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid="],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1, check=False,
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
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pid <= 1 or pid >= _MAX_PID:
        return None
    return pid


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(raw: Any, now: datetime) -> float | None:
    parsed = _parse_time(raw)
    if parsed is None:
        return None
    try:
        return round((now - parsed).total_seconds(), 3)
    except (OverflowError, ValueError):
        return None


def _read_record(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(object, evidence)``. A bad file is an empty object plus an explicit status, never healthy input."""
    text, evidence = _read_bounded(path)
    if evidence != "ok" or text is None:
        return {}, evidence
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}, "malformed"
    if not isinstance(payload, dict):
        return {}, "malformed"
    return payload, "ok"


def _read_json(path: Path) -> dict[str, Any] | None:
    payload, evidence = _read_record(path)
    return payload if evidence == "ok" else None


def _read_bounded(path: Path, limit: int = _READ_LIMIT) -> tuple[str | None, str]:
    """Read one regular file, at most ``limit`` bytes, without following a final symlink or blocking on a FIFO."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unavailable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "unavailable"
    if info.st_size > limit:
        return None, "oversized"
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, "unavailable"
    try:
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            try:
                data = os.read(fd, min(remaining, 65536))
            except OSError:
                return None, "unavailable"
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
    finally:
        os.close(fd)
    blob = b"".join(chunks)
    if len(blob) > limit:
        return None, "oversized"
    try:
        return blob.decode("utf-8-sig"), "ok"
    except UnicodeError:
        return None, "malformed"
