"""Hostctl receipt: kernel start, loop timestamp, and no ready claim without the loop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import gateway.application_readiness as readiness


def test_linux_start_ticks_match_the_hostctl_division():
    stat = "42 (python) R " + " ".join(["0"] * 18) + " 5000\n"
    started = readiness.parse_linux_start(stat, 1_700_000_000, 100)
    assert started == datetime.fromtimestamp(1_700_000_050, timezone.utc)
    assert readiness.parse_linux_start("9 (zombie) Z " + " ".join(["0"] * 18) + " 1\n", 1, 100) is None


def test_darwin_lstart_parses_the_hostctl_sample():
    started = readiness.parse_darwin_lstart("S Thu Oct  8 23:48:08 2026\n")
    assert started == datetime(2026, 10, 8, 23, 48, 8, tzinfo=timezone.utc)


def test_ready_receipt_keeps_the_loop_timestamp(tmp_path: Path):
    loop_at = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    path = readiness.publish_application_readiness(
        phase="ready", event_loop_at=loop_at, home=tmp_path, pid=os.getpid(),
    )
    assert path is not None and path.is_file() and not path.is_symlink()
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    assert document["schema_version"] == "1"
    assert document["application_id"] == "hermes-gateway"
    assert document["phase"] == "ready"
    assert document["pid"] == os.getpid()
    assert document["signals"] == [{"name": "event_loop", "updated_at": "2026-10-08T16:00:00Z"}]
    assert document["pid_started_at"].endswith("Z")
    assert document["updated_at"].endswith("Z")
    assert set(document) <= {
        "schema_version", "application_id", "revision", "phase", "pid",
        "pid_started_at", "updated_at", "phase_since", "exit_reason",
        "progress", "signals", "references",
    }


def test_ready_without_a_loop_timestamp_is_not_ready(tmp_path: Path):
    path = readiness.publish_application_readiness(phase="ready", home=tmp_path, pid=os.getpid())
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    assert document["phase"] == "starting"
    assert "signals" not in document


def test_unknown_kernel_start_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(readiness, "kernel_started_at", lambda pid: None)
    assert readiness.publish_application_readiness(
        phase="ready",
        event_loop_at=datetime.now(timezone.utc),
        home=tmp_path,
        pid=os.getpid(),
    ) is None
    assert not readiness.receipt_path(tmp_path).exists()


def test_stopped_receipt_drops_the_loop_signal(tmp_path: Path):
    path = readiness.publish_application_readiness(
        phase="stopped",
        exit_reason="gateway loop stopped",
        event_loop_at=datetime.now(timezone.utc),
        home=tmp_path,
        pid=os.getpid(),
    )
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    assert document["phase"] == "stopped"
    assert document["exit_reason"] == "gateway loop stopped"
    assert "signals" not in document


def test_symlink_receipt_is_replaced_by_a_regular_file(tmp_path: Path):
    path = readiness.receipt_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "elsewhere")
    written = readiness.publish_application_readiness(
        phase="starting", home=tmp_path, pid=os.getpid(),
    )
    assert written == path
    assert path.is_file() and not path.is_symlink()


def test_this_process_start_matches_the_native_observation():
    started = readiness.kernel_started_at(os.getpid())
    assert started is not None
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["/bin/ps", "-p", str(os.getpid()), "-o", "state=,lstart="],
            capture_output=True, text=True, encoding="utf-8", timeout=2,
            env={"LC_ALL": "C", "LANG": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"},
            check=True,
        )
        assert started == readiness.parse_darwin_lstart(completed.stdout)
    elif sys.platform == "linux":
        stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8-sig")
        btime = next(
            int(line.split()[1]) for line in Path("/proc/stat").read_text(encoding="utf-8-sig").splitlines()
            if line.startswith("btime ")
        )
        hz = int(os.sysconf("SC_CLK_TCK"))
        assert started == readiness.parse_linux_start(stat, btime, hz)
