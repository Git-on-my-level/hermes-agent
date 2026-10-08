"""A bounded serving sync kills its process group instead of waiting out the tail."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hermes_cli.serving_recovery import PhaseDeadlineExceeded, _wait_process_group


def test_deadline_kills_the_sync_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"handle = open({str(pidfile)!r}, 'w')\n"
        "handle.write(str(child.pid))\n"
        "handle.close()\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists(), "sync child did not publish its worker pid"
        grandchild = int(pidfile.read_text(encoding="utf-8"))
        with pytest.raises(PhaseDeadlineExceeded) as caught:
            _wait_process_group(process, 0.2, phase="dependency_sync")
        assert caught.value.phase == "dependency_sync"
        _assert_dead(process.pid)
        _assert_dead(grandchild)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _assert_dead(pid: int) -> None:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} was still alive after the phase deadline")
