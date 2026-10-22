"""Bounded dependency recovery for a supervised gateway that must serve now.

A watchdog restart used to enter the full source-update tail (dependency sync, product
builds, post-update maintenance) before the process reached its event loop. That tail is
still required for a finished update, but it is not required to boot the last committed
dependency generation. This module:

* publishes a blocking maintenance record while the sync runs, with its own deadline
* runs the sync in a child process group so a stuck package worker can be killed
* leaves the product/maintenance tail owed (``source-completion-pending``) for
  ``hermes update``
* never prints success and never clears that obligation on a deadline miss

The selected generation commits only when the sync child exits 0. Killing the child
before that leaves the previous generation in place.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A serving restart may spend this long proving the new dependency generation. It is
# this phase's deadline, not a raised global timeout: the product tail is not in it.
SERVING_DEPENDENCY_SYNC_DEADLINE_S = 45.0

_MAINTENANCE_RELATIVE = ("state", "gateway-maintenance.json")
_RECOVERY = "run `hermes update` from a shell to finish it"


class PhaseDeadlineExceeded(RuntimeError):
    """A named startup phase did not finish before its own deadline."""

    def __init__(self, phase: str, deadline_s: float):
        self.phase = phase
        self.deadline_s = deadline_s
        super().__init__(f"{phase} exceeded {deadline_s:g}s")


def maintenance_path(home: Path | None = None) -> Path:
    from hermes_constants import get_process_hermes_home

    base = home if home is not None else get_process_hermes_home()
    return base.joinpath(*_MAINTENANCE_RELATIVE)


def publish_maintenance(
    *,
    phase: str,
    blocking: bool,
    deadline_s: float | None = None,
    waiting_on: str = "",
    reason: str = "",
    home: Path | None = None,
) -> Path:
    """Write the maintenance record the read-only health probe reports. Never raises."""
    now = datetime.now(timezone.utc)
    payload: dict = {
        "schema": 1,
        "phase": phase,
        "blocking": blocking,
        "pid": os.getpid(),
        "started_at": now.isoformat(),
        "waiting_on": waiting_on,
        "reason": reason,
        "recovery": _RECOVERY,
    }
    if deadline_s is not None:
        payload["deadline_s"] = deadline_s
        payload["deadline_at"] = (now + timedelta(seconds=deadline_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    from gateway.application_readiness import kernel_started_at, rfc3339_utc

    started = kernel_started_at(os.getpid())
    if started is not None:
        payload["pid_started_at"] = rfc3339_utc(started)
    path = maintenance_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return path
    return path


def clear_maintenance(home: Path | None = None) -> None:
    try:
        maintenance_path(home).unlink(missing_ok=True)
    except OSError:
        return


def _publish_recovery_receipt(*, blocking: bool, summary: str, phase: str = "maintenance") -> None:
    """Project this recovery phase into the hostctl receipt. No event-loop signal."""
    from gateway.application_readiness import publish_application_readiness

    deadline = None
    if blocking:
        deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=SERVING_DEPENDENCY_SYNC_DEADLINE_S)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    publish_application_readiness(
        phase=phase,
        progress={"summary": summary, "deadline_at": deadline} if deadline else {"summary": summary},
    )


def recover_serving_dependencies(root: Path) -> None:
    """Sync dependencies under a deadline, then return so the gateway can serve.

    The product tail is not run here. A deadline miss raises; the caller (bootstrap)
    continues on the previous generation and the marker stays owed.
    """
    from hermes_cli.venv_sync import (
        _sync_source_dependencies,
        _tree_matches_completed_stamp,
        clear_completion,
    )

    root = Path(root)
    completed = _tree_matches_completed_stamp(root)
    publish_maintenance(
        phase="dependency_sync",
        blocking=True,
        deadline_s=SERVING_DEPENDENCY_SYNC_DEADLINE_S,
        waiting_on="package worker",
    )
    _publish_recovery_receipt(blocking=True, summary="dependency sync")
    try:
        run_bounded_dependency_sync(
            root,
            arm=not completed,
            deadline=SERVING_DEPENDENCY_SYNC_DEADLINE_S,
        )
    except PhaseDeadlineExceeded:
        publish_maintenance(
            phase="dependency_sync",
            blocking=False,
            deadline_s=SERVING_DEPENDENCY_SYNC_DEADLINE_S,
            waiting_on="package worker",
            reason="deadline exceeded",
        )
        _publish_recovery_receipt(blocking=False, summary="dependency sync deadline exceeded")
        raise RuntimeError(
            "dependency sync exceeded its "
            f"{SERVING_DEPENDENCY_SYNC_DEADLINE_S:g}s deadline; "
            f"serving the last committed dependencies — {_RECOVERY}"
        ) from None
    except Exception:
        publish_maintenance(
            phase="dependency_sync",
            blocking=False,
            waiting_on="package worker",
            reason="sync failed",
        )
        _publish_recovery_receipt(blocking=False, summary="dependency sync failed")
        raise
    if completed:
        # The stamp already names this tree. A stale venv was re-provisioned;
        # there is no product tail to owe.
        clear_completion(root)
    clear_maintenance()
    _publish_recovery_receipt(blocking=False, summary="dependencies synced; gateway loop not up", phase="starting")


def run_bounded_dependency_sync(root: Path, *, arm: bool, deadline: float) -> None:
    """Run ``_sync_source_dependencies`` in its own process group, bounded by ``deadline``."""
    from pm.environments import activation_environment

    command = [
        sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()),
        "--source", str(root), "--arm", "1" if arm else "0",
    ]
    process = subprocess.Popen(
        command,
        cwd=root,
        env=activation_environment(root),
        start_new_session=True,
    )
    code = _wait_process_group(process, deadline, phase="dependency_sync")
    if code != 0:
        raise RuntimeError(
            "source update dependency sync failed; "
            f"{_RECOVERY}"
        )


def _wait_process_group(process: subprocess.Popen, deadline: float, *, phase: str) -> int:
    try:
        return process.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        raise PhaseDeadlineExceeded(phase, deadline) from None


def _kill_process_group(process: subprocess.Popen) -> None:
    """Stop the sync child and its descendants. psutil is cross-platform; no killpg/SIGKILL."""
    try:
        import psutil

        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for child in descendants:
            child.kill()
        parent.kill()
    except Exception:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded serving-gateway dependency sync")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arm", choices=("0", "1"), required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = args.source.resolve()
    sys.path.insert(0, str(root))
    from hermes_cli.venv_sync import _sync_source_dependencies

    _sync_source_dependencies(root, arm=args.arm == "1")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
