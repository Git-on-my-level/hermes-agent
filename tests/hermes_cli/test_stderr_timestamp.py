"""Tests for hermes_cli.stderr_timestamp."""

import re
import sys

from gateway.restart import EXTERNAL_GATEWAY_SUPERVISOR_ENV
from hermes_cli import stderr_timestamp

_STALE_GATEWAY_ARGV = [
    sys.executable,
    "-m",
    "hermes_cli.main",
    "gateway",
    "run",
    "--replace",
]
_LAUNCHD_ENV = {"PATH": "/usr/bin", "XPC_SERVICE_NAME": "ai.hermes.gateway-butler"}


def test_main_timestamps_each_stderr_line(tmp_path):
    log_path = tmp_path / "gateway.error.log"
    code = (
        "import sys\n"
        "sys.stderr.write('first failure\\n')\n"
        "sys.stderr.write('second failure without newline\\n')\n"
        "sys.stderr.write('2026-07-15 12:34:56,789 already timestamped')\n"
        "sys.exit(7)\n"
    )

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 7
    lines = log_path.read_text(encoding="utf-8").splitlines()
    timestamp = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
    assert len(lines) == 3
    assert re.fullmatch(f"{timestamp} first failure", lines[0])
    assert re.fullmatch(f"{timestamp} second failure without newline", lines[1])
    assert lines[2] == "2026-07-15 12:34:56,789 already timestamped"


def test_prepare_upgrades_stale_gateway_argv_under_launchd():
    upgraded = stderr_timestamp._prepare_child_command(
        _STALE_GATEWAY_ARGV, _LAUNCHD_ENV
    )
    assert upgraded == [*_STALE_GATEWAY_ARGV, "--external-supervisor"]


def test_prepare_keeps_existing_external_supervisor_flag():
    already = [*_STALE_GATEWAY_ARGV, "--external-supervisor"]
    assert (
        stderr_timestamp._prepare_child_command(already, _LAUNCHD_ENV) == already
    )


def test_prepare_skips_arbitrary_command_under_launchd():
    """A generic wrapper must not mark random launchd children as the gateway."""
    other = [sys.executable, "-c", "print('ok')"]
    assert stderr_timestamp._prepare_child_command(other, _LAUNCHD_ENV) == other


def test_prepare_skips_interactive_xpc_zero_even_for_gateway_argv():
    assert (
        stderr_timestamp._prepare_child_command(
            _STALE_GATEWAY_ARGV, {"PATH": "/usr/bin", "XPC_SERVICE_NAME": "0"}
        )
        == _STALE_GATEWAY_ARGV
    )
    assert (
        stderr_timestamp._prepare_child_command(_STALE_GATEWAY_ARGV, {"PATH": "/usr/bin"})
        == _STALE_GATEWAY_ARGV
    )


def test_main_injects_flag_into_stale_gateway_child(tmp_path, monkeypatch):
    """Stale plist inner argv must grow --external-supervisor in the grandchild."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway-butler")
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    log_path = tmp_path / "gateway.error.log"
    marker_path = tmp_path / "argv.txt"
    code = (
        "import sys\n"
        f"from pathlib import Path\n"
        f"Path({str(marker_path)!r}).write_text("
        "'\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
    )
    stale = [sys.executable, "-c", code, "-m", "hermes_cli.main", "gateway", "run", "--replace"]

    rc = stderr_timestamp.main(
        ["--error-log", str(log_path), "--", *stale]
    )

    assert rc == 0
    recorded = marker_path.read_text(encoding="utf-8").splitlines()
    assert recorded[-1] == "--external-supervisor"
    assert "gateway" in recorded and "run" in recorded


def test_main_does_not_mark_arbitrary_launchd_child(tmp_path, monkeypatch):
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway-butler")
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    log_path = tmp_path / "gateway.error.log"
    marker_path = tmp_path / "marker.txt"
    code = (
        "import os\n"
        f"from pathlib import Path\n"
        f"Path({str(marker_path)!r}).write_text("
        f"os.environ.get({EXTERNAL_GATEWAY_SUPERVISOR_ENV!r}, 'unset'), encoding='utf-8')\n"
    )

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 0
    assert marker_path.read_text(encoding="utf-8") == "unset"


def test_main_does_not_mark_unsupervised_child(tmp_path, monkeypatch):
    """Foreground/unsupervised starts must not inherit a fabricated marker."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    log_path = tmp_path / "gateway.error.log"
    marker_path = tmp_path / "marker.txt"
    code = (
        "import os\n"
        f"from pathlib import Path\n"
        f"Path({str(marker_path)!r}).write_text("
        f"os.environ.get({EXTERNAL_GATEWAY_SUPERVISOR_ENV!r}, 'unset'), encoding='utf-8')\n"
    )

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 0
    assert marker_path.read_text(encoding="utf-8") == "unset"


def _rotating_log(tmp_path, *, max_bytes, backup_count):
    return stderr_timestamp.RotatingErrorLog(
        tmp_path / "gateway.error.log", max_bytes=max_bytes, backup_count=backup_count
    )


def test_error_log_stays_within_the_configured_budget(tmp_path):
    """The launchd error log is bounded by max_bytes * (backup_count + 1), not unbounded.

    Regression: gateway.error.log had no rotation at all and reached 40 MB in the field while
    logging.max_size_mb/backup_count correctly bounded agent.log.
    """
    log_path = tmp_path / "gateway.error.log"
    max_bytes, backup_count = 512, 2

    with _rotating_log(tmp_path, max_bytes=max_bytes, backup_count=backup_count) as log:
        for index in range(400):
            log.write_line(f"line {index} " + "x" * 60)

    rotated = sorted(tmp_path.glob("gateway.error.log.*"))
    assert [p.name for p in rotated] == ["gateway.error.log.1", "gateway.error.log.2"]
    total = sum(p.stat().st_size for p in [log_path, *rotated])
    assert total <= max_bytes * (backup_count + 1)


def test_rotation_preserves_the_newest_lines_and_discards_the_oldest(tmp_path):
    """Rollover shifts .1 -> .2 and drops beyond backup_count; the live file holds the newest."""
    log_path = tmp_path / "gateway.error.log"

    with _rotating_log(tmp_path, max_bytes=200, backup_count=1) as log:
        for index in range(200):
            log.write_line(f"failure {index}")

    assert "failure 199" in log_path.read_text(encoding="utf-8")
    assert "failure 0" not in log_path.read_text(encoding="utf-8")
    assert (tmp_path / "gateway.error.log.1").exists()
    assert not (tmp_path / "gateway.error.log.2").exists()


def test_rotation_settings_follow_logging_config(monkeypatch):
    """The wrapper rotates on the same logging.* knobs that bound agent.log."""
    import hermes_logging

    monkeypatch.setattr(
        hermes_logging, "_read_logging_config", lambda: ("INFO", 7, 4)
    )
    assert stderr_timestamp._rotation_settings() == (7 * 1024 * 1024, 4)


def test_rotation_settings_fall_back_when_config_is_unreadable(monkeypatch):
    """A gateway must still start when config.yaml cannot be read; defaults apply."""
    import hermes_logging

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(hermes_logging, "_read_logging_config", _boom)
    assert stderr_timestamp._rotation_settings() == (
        stderr_timestamp.DEFAULT_MAX_BYTES,
        stderr_timestamp.DEFAULT_BACKUP_COUNT,
    )


def test_oversized_single_line_is_written_rather_than_looping(tmp_path):
    """A line larger than the whole budget lands in the log instead of rotating forever."""
    log_path = tmp_path / "gateway.error.log"

    with _rotating_log(tmp_path, max_bytes=64, backup_count=1) as log:
        log.write_line("y" * 4096)

    assert "y" * 4096 in log_path.read_text(encoding="utf-8")


def test_main_rotates_child_stderr_through_the_wrapper(tmp_path, monkeypatch):
    """End-to-end: a chatty child cannot grow the error log past the configured budget."""
    monkeypatch.setattr(stderr_timestamp, "_rotation_settings", lambda: (1024, 1))
    log_path = tmp_path / "gateway.error.log"
    code = (
        "import sys\n"
        "for index in range(500):\n"
        "    sys.stderr.write('noisy %d\\n' % index)\n"
    )

    rc = stderr_timestamp.main(
        ["--error-log", str(log_path), "--", sys.executable, "-c", code]
    )

    assert rc == 0
    sizes = [p.stat().st_size for p in [log_path, tmp_path / "gateway.error.log.1"]]
    assert max(sizes) <= 1024 + 256
    assert "noisy 499" in log_path.read_text(encoding="utf-8")
