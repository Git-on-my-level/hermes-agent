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
    from gateway.application_readiness import kernel_started_at, rfc3339_utc

    now = datetime.now(timezone.utc)
    pid = os_pid()
    started = kernel_started_at(pid)
    _write(tmp_path / "gateway_state.json", {
        "pid": 2**22,
        "gateway_state": "running",
        "updated_at": (now - timedelta(hours=1)).isoformat(),
    })
    record = {
        "schema": 1,
        "blocking": True,
        "phase": "dependency_sync",
        "pid": pid,
        "started_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=45)).isoformat(),
        "waiting_on": "package worker",
        "recovery": "run `hermes update` from a shell to finish it",
    }
    if started is not None:
        record["pid_started_at"] = rfc3339_utc(started)
    _write(tmp_path / "state" / "gateway-maintenance.json", record)

    document = collect_service_health(tmp_path, now=now)

    if started is None:
        assert document["phase"] != "maintenance"
        assert document["maintenance"]["trusted"] is False
    else:
        assert document["phase"] == "maintenance"
        assert document["maintenance"]["trusted"] is True
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


def test_future_heartbeat_is_not_ready(tmp_path: Path):
    now = datetime.now(timezone.utc)
    pid = os_pid()
    fingerprint = _start_fingerprint(pid)
    _write(tmp_path / "gateway_state.json", {
        "pid": pid,
        "start_time": fingerprint,
        "gateway_state": "running",
        "updated_at": now.isoformat(),
        "platforms": {"telegram": {"state": "connected", "updated_at": now.isoformat()}},
    })
    _write(tmp_path / "state" / "gateway.heartbeat", {
        "pid": pid,
        "updated_at": (now + timedelta(hours=1)).isoformat(),
    })

    document = collect_service_health(tmp_path, now=now)

    assert document["phase"] == "degraded"
    assert document["healthy"] is False
    assert document["freshness"]["heartbeat_fresh"] is False
    assert document["freshness"]["heartbeat_state"] == "indeterminate"


def test_draining_gateway_is_not_ready(tmp_path: Path):
    now = datetime.now(timezone.utc)
    pid = os_pid()
    _write(tmp_path / "gateway_state.json", {
        "pid": pid,
        "start_time": _start_fingerprint(pid),
        "gateway_state": "draining",
        "updated_at": now.isoformat(),
    })
    _write(tmp_path / "state" / "gateway.heartbeat", {"pid": pid, "updated_at": now.isoformat()})

    document = collect_service_health(tmp_path, now=now)

    assert document["phase"] == "degraded"
    assert document["healthy"] is False


def test_unverified_maintenance_does_not_override_the_gateway(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write(tmp_path / "gateway_state.json", {
        "pid": 2**22,
        "gateway_state": "running",
        "updated_at": now.isoformat(),
    })
    _write(tmp_path / "state" / "gateway-maintenance.json", {
        "blocking": True,
        "pid": os_pid(),
        "started_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=30)).isoformat(),
    })

    document = collect_service_health(tmp_path, now=now)

    assert document["maintenance"]["trusted"] is False
    assert document["phase"] != "maintenance"


def test_fifo_and_oversized_markers_are_not_healthy(tmp_path: Path):
    import os
    import time

    state = tmp_path / "gateway_state.json"
    state.write_bytes(b"{" + b"x" * (_oversized()) )
    began = time.monotonic()
    document = collect_service_health(tmp_path, now=datetime.now(timezone.utc))
    assert time.monotonic() - began < 1
    assert document["evidence"]["state"] == "oversized"
    assert document["phase"] == "degraded"
    assert document["healthy"] is False

    if sys.platform == "win32":
        return
    heartbeat = tmp_path / "state" / "gateway.heartbeat"
    heartbeat.parent.mkdir(parents=True)
    os.mkfifo(heartbeat)
    began = time.monotonic()
    probed = collect_service_health(tmp_path, now=datetime.now(timezone.utc))
    assert time.monotonic() - began < 1
    assert probed["evidence"]["heartbeat"] == "unavailable"
    assert probed["healthy"] is False


def _oversized() -> int:
    from gateway.service_health import _READ_LIMIT
    return _READ_LIMIT + 10


def test_profile_scan_skips_foreign_values_and_passthrough():
    from hermes_cli._parser import _OPTIONAL_VALUE_FLAGS_FALLBACK, _VALUE_FLAGS_FALLBACK
    from hermes_cli.profile_argv import scan_profile_flag

    flags, optional = _VALUE_FLAGS_FALLBACK, _OPTIONAL_VALUE_FLAGS_FALLBACK
    assert scan_profile_flag(["--model", "-p", "gateway", "health"], flags, optional).name is None
    assert scan_profile_flag(["gateway", "health", "--", "--profile", "beta"], flags, optional).name is None
    assert scan_profile_flag(
        ["mcp", "add", "srv", "--args", "docker", "--profile", "other"], flags, optional,
    ).name is None
    selected = scan_profile_flag(
        ["-m", "dummy", "--profile", "beta", "gateway", "health"], flags, optional,
    )
    assert selected.name == "beta"


def test_nonfinite_pid_does_not_raise(tmp_path: Path):
    (tmp_path / "gateway_state.json").write_text(
        '{"pid": 1e309, "gateway_state": "running"}', encoding="utf-8",
    )
    document = collect_service_health(tmp_path, now=datetime.now(timezone.utc))
    assert document["healthy"] is False
    assert document["phase"] != "ready"


def test_gateway_health_entry_point_follows_explicit_and_saved_profile(tmp_path: Path):
    """Real entry: explicit A→B→A, then a saved profile, without borrowing a bad one."""
    import os

    root = tmp_path / "hermes-home"
    other = root / "profiles" / "beta"
    root.mkdir()
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "gateway_state.json").write_text(
        json.dumps({"code_sha": "canary-a", "gateway_state": "stopped"}), encoding="utf-8",
    )
    (other / "gateway_state.json").write_text(
        json.dumps({"code_sha": "canary-b", "gateway_state": "stopped"}), encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    for key in (
        "HERMES_PROFILE",
        "HERMES_SUPERVISED_CHILD",
        "HERMES_S6_SUPERVISED_CHILD",
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
        "INVOCATION_ID",
    ):
        env.pop(key, None)

    def invoke(args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", *args],
            cwd=repo,
            env=env | extra,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def sees(result: subprocess.CompletedProcess[str], code: str, other_code: str) -> None:
        assert result.returncode == 0, result.stderr
        assert code in result.stdout and other_code not in result.stdout

    sees(invoke(["gateway", "health"]), "canary-a", "canary-b")
    sees(invoke(["--profile", "beta", "gateway", "health"]), "canary-b", "canary-a")
    sees(invoke(["--profile", "default", "gateway", "health"]), "canary-a", "canary-b")

    (root / "active_profile").write_text("beta\n", encoding="utf-8")
    sees(invoke(["gateway", "health"]), "canary-b", "canary-a")
    sees(invoke(["-m", "dummy", "--profile", "default", "gateway", "health"]), "canary-a", "canary-b")
    sees(invoke(["gateway", "health"]), "canary-b", "canary-a")
    sees(invoke(["--reasoning", "high", "gateway", "health"]), "canary-b", "canary-a")
    sees(invoke(["--model", "-p", "gateway", "health"]), "canary-b", "canary-a")

    (root / "active_profile").write_text("default\n", encoding="utf-8")
    sees(invoke(["gateway", "health", "--", "--profile", "beta"]), "canary-a", "canary-b")

    refused = invoke(["--profile", "gone", "gateway", "health"])
    assert refused.returncode == 2, refused.stderr
    assert "canary-a" not in refused.stdout and "canary-b" not in refused.stdout

    (root / "active_profile").write_text("gone\n", encoding="utf-8")
    stale = invoke(["gateway", "health"])
    assert stale.returncode == 2, stale.stderr
    assert "canary-a" not in stale.stdout and "canary-b" not in stale.stdout

    (root / "active_profile").write_text("beta\n", encoding="utf-8")
    sees(invoke(["gateway", "health"], HERMES_SUPERVISED_CHILD="1"), "canary-a", "canary-b")

    assert _files(root) == {
        "gateway_state.json",
        "active_profile",
        "profiles/beta/config.yaml",
        "profiles/beta/gateway_state.json",
    }
    assert _files(other) == {"config.yaml", "gateway_state.json"}


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def os_pid() -> int:
    import os
    return os.getpid()
