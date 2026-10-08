"""Read-only service health stays honest when the gateway process is dead or not ready."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.service_health import (
    _start_fingerprint,
    collect_service_health,
    command_tokens,
    maybe_run_readonly_health,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_gateway_health_argv_is_recognized_without_the_cli_parser():
    assert command_tokens(["-p", "coder", "gateway", "health"]) == ["gateway", "health"]
    assert command_tokens(["gateway", "status"]) == ["gateway", "status"]
    assert maybe_run_readonly_health(["gateway", "status"]) is None


def test_dead_pid_claiming_running_is_stale(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = proc.pid
    fingerprint = _start_fingerprint(pid)
    proc.kill()
    proc.wait(timeout=5)
    now = datetime.now(timezone.utc)
    _write(tmp_path / "gateway_state.json", {
        "pid": pid,
        "start_time": fingerprint,
        "gateway_state": "running",
        "code_sha": "abc",
        "updated_at": now.isoformat(),
        "platforms": {"telegram": {"state": "connected", "updated_at": now.isoformat()}},
    })
    _write(tmp_path / "gateway.pid", {"pid": pid, "start_time": fingerprint})
    _write(tmp_path / "state" / "gateway.heartbeat", {"pid": pid, "updated_at": now.isoformat()})
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=secret-token-value\n", encoding="utf-8")

    document = collect_service_health(tmp_path, now=now)

    assert document["phase"] == "stale"
    assert document["healthy"] is False
    assert "secret-token-value" not in json.dumps(document)
    assert document["application_readiness"] is None
    assert not (tmp_path / "state" / "application-readiness.json").exists()


def test_live_verified_gateway_with_a_fresh_heartbeat_is_ready(tmp_path: Path):
    now = datetime.now(timezone.utc)
    pid = os_pid()
    fingerprint = _start_fingerprint(pid)
    _write(tmp_path / "gateway_state.json", {
        "pid": pid,
        "start_time": fingerprint,
        "gateway_state": "running",
        "code_sha": "running-sha",
        "updated_at": now.isoformat(),
        "platforms": {"telegram": {"state": "connected", "updated_at": now.isoformat()}},
    })
    _write(tmp_path / "state" / "gateway.heartbeat", {
        "pid": pid, "updated_at": now.isoformat(),
    })
    _write(tmp_path / "install-stamp.json", {"commit": "installed-sha"})

    document = collect_service_health(tmp_path, source_root=tmp_path, now=now)

    if fingerprint is None:
        assert document["phase"] == "degraded"
        assert document["identity"]["verified"] is False
    else:
        assert document["phase"] == "ready"
        assert document["identity"]["verified"] is True
        assert document["healthy"] is True
    assert document["revisions"]["running"] == "running-sha"
    assert document["revisions"]["installed"] == "installed-sha"
    assert document["revisions"]["match"] is False
    assert document["platforms"][0]["connected"] is True


def test_blocking_maintenance_outranks_a_stale_running_record(tmp_path: Path):
    now = datetime.now(timezone.utc)
    pid = os_pid()
    _write(tmp_path / "gateway_state.json", {
        "pid": 2**22,
        "gateway_state": "running",
        "updated_at": (now - timedelta(hours=1)).isoformat(),
    })
    _write(tmp_path / "state" / "gateway-maintenance.json", {
        "schema": 1,
        "blocking": True,
        "phase": "dependency_sync",
        "pid": pid,
        "deadline_at": (now + timedelta(seconds=45)).isoformat(),
        "waiting_on": "package worker",
        "recovery": "run `hermes update` from a shell to finish it",
    })

    document = collect_service_health(tmp_path, now=now)

    assert document["phase"] == "maintenance"
    assert document["healthy"] is False
    assert document["maintenance"]["waiting_on"] == "package worker"
    assert document["update"]["recovery"]


def test_loop_tick_answer_is_reported():
    if sys.platform == "win32":
        return
    # macOS rejects AF_UNIX paths longer than 104 bytes; pytest's tmp_path exceeds that.
    import tempfile
    now = datetime.now(timezone.utc)
    pid = os_pid()
    fingerprint = _start_fingerprint(pid)
    tmp_path = Path(tempfile.mkdtemp(prefix="hs", dir="/tmp"))
    sock_path = tmp_path / "state" / f"gateway.loop-tick.{pid}.sock"
    sock_path.parent.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    server.settimeout(2)

    def _serve():
        try:
            conn, _addr = server.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"1")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        _write(tmp_path / "gateway_state.json", {
            "pid": pid,
            "start_time": fingerprint,
            "gateway_state": "running",
            "updated_at": now.isoformat(),
        })
        document = collect_service_health(tmp_path, now=now)
    finally:
        server.close()
        thread.join(timeout=2)
        shutil.rmtree(tmp_path, ignore_errors=True)

    assert document["freshness"]["loop_tick"] == "answered"


def os_pid() -> int:
    import os
    return os.getpid()
