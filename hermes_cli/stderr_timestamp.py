"""Run a child process while prefixing each stderr line with a timestamp.

The log this writes is the launchd gateway's ``StandardErrorPath`` target, so it is also the
only place that can bound its size: launchd has no rotation of its own and the file is not a
``logging`` handler. Rollover therefore happens here, on the same ``logging.max_size_mb`` /
``logging.backup_count`` settings that bound ``agent.log``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Sequence, TextIO

EXTERNAL_SUPERVISOR_FLAG = "--external-supervisor"

_TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}(?:\s|$)")


def _timestamp() -> str:
    """Match logging.Formatter's default ``%(asctime)s`` timestamp shape."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:23]


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


def _rotation_settings() -> tuple[int, int]:
    """``(max_bytes, backup_count)`` from ``logging.*`` in config.yaml, else the shipped floor.

    launchd starts this wrapper before anything guarantees the Hermes config is readable, and a
    gateway that cannot start is far worse than one whose error log rotates on the defaults — so
    any failure here falls back rather than propagating.
    """
    try:
        from hermes_logging import configured_log_rotation
        return configured_log_rotation()
    except Exception:
        return DEFAULT_MAX_BYTES, DEFAULT_BACKUP_COUNT


class RotatingErrorLog:
    """Append timestamped lines to *path*, rolling over at *max_bytes* into ``.1``…``.N``.

    ``gateway.error.log`` is written by this wrapper rather than by the logging subsystem, so the
    ``logging.max_size_mb`` / ``logging.backup_count`` rotation that bounds ``agent.log`` never
    reached it and the launchd error log grew without limit (40 MB in the field). The rollover is
    open-coded instead of delegated to ``RotatingFileHandler`` because the payload is raw child
    stderr, not ``LogRecord``s.
    """

    def __init__(self, path: Path, *, max_bytes: int, backup_count: int) -> None:
        self._path = path
        self._max_bytes = max(0, int(max_bytes))
        self._backup_count = max(0, int(backup_count))
        self._file: TextIO | None = None
        self._size = 0

    def __enter__(self) -> "RotatingErrorLog":
        self._open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8", buffering=1)
        try:
            self._size = self._path.stat().st_size
        except OSError:
            self._size = 0

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    def write_line(self, line: str) -> None:
        rendered = line.rstrip("\r\n")
        prefix = "" if _TIMESTAMP_PREFIX.match(rendered) else f"{_timestamp()} "
        payload = f"{prefix}{rendered}\n"
        width = len(payload.encode("utf-8"))
        self._roll_if_full(width)
        if self._file is None:
            return
        self._file.write(payload)
        self._size += width

    def _roll_if_full(self, incoming: int) -> None:
        # A non-empty file is the precondition: a single line longer than the whole budget must
        # land somewhere instead of rotating an empty file on every write.
        if not self._max_bytes or not self._size or self._size + incoming <= self._max_bytes:
            return
        self.close()
        try:
            self._roll()
        except OSError:
            # Losing the gateway's stderr is worse than an oversized log; keep appending.
            pass
        self._open()

    def _roll(self) -> None:
        name = self._path.name
        if not self._backup_count:
            self._path.unlink(missing_ok=True)
            return
        self._path.with_name(f"{name}.{self._backup_count}").unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self._path.with_name(f"{name}.{index}")
            if source.exists():
                source.replace(self._path.with_name(f"{name}.{index + 1}"))
        self._path.replace(self._path.with_name(f"{name}.1"))


def _open_log(log_path: Path) -> RotatingErrorLog:
    max_bytes, backup_count = _rotation_settings()
    return RotatingErrorLog(log_path, max_bytes=max_bytes, backup_count=backup_count)


def _copy_stderr_with_timestamps(stderr: BinaryIO, log_path: Path) -> None:
    with _open_log(log_path) as log_file:
        for raw_line in iter(stderr.readline, b""):
            log_file.write_line(raw_line.decode("utf-8", errors="replace"))


def _install_signal_forwarders(proc: subprocess.Popen[bytes]) -> dict[int, object]:
    def _forward(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
        if signum is not None:
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, _forward)
            except (OSError, RuntimeError, ValueError):
                previous.pop(signum, None)
    return previous


def _is_hermes_gateway_run_argv(command: Sequence[str]) -> bool:
    """True for Hermes ``gateway run`` argv this wrapper is allowed to upgrade.

    The wrapper is generic. Only historical/current Hermes gateway shapes get ``--external-
    supervisor``; an arbitrary launchd child must not be marked as gateway-supervised (#87005).
    """
    try:
        from gateway.status import looks_like_gateway_command_line
    except Exception:
        return False
    return bool(looks_like_gateway_command_line(" ".join(str(part) for part in command)))


def _prepare_child_command(command: Sequence[str], environ: Mapping[str, str] | None = None) -> list[str]:
    """Return the argv to exec, upgrading stale launchd-wrapped gateway commands.

    launchd stamps ``XPC_SERVICE_NAME=<job label>`` only on this wrapper (its direct child; an
    interactive shell has none, the grandchild sees ``XPC_SERVICE_NAME=0``). Newly generated
    plists put ``--external-supervisor`` on the inner ``gateway run`` so ``hermes update`` can see
    the flag on the live process argv.
    """
    argv = [str(part) for part in command]
    env = os.environ if environ is None else environ
    xpc_service = str(env.get("XPC_SERVICE_NAME", "")).strip()
    if EXTERNAL_SUPERVISOR_FLAG not in argv and xpc_service and xpc_service != "0" and _is_hermes_gateway_run_argv(argv):
        argv.append(EXTERNAL_SUPERVISOR_FLAG)
    return argv


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a command and timestamp each stderr line into a log file.")
    parser.add_argument("--error-log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing command after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path: Path = args.error_log

    try:
        proc = subprocess.Popen(_prepare_child_command(args.command), stderr=subprocess.PIPE)
    except OSError as exc:
        with _open_log(log_path) as log_file:
            log_file.write_line(f"failed to start stderr-timestamped command: {exc}")
        return 127

    assert proc.stderr is not None
    previous_handlers = _install_signal_forwarders(proc)
    try:
        _copy_stderr_with_timestamps(proc.stderr, log_path)
    finally:
        proc.stderr.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    returncode = proc.wait()
    return 128 + abs(returncode) if returncode < 0 else returncode


if __name__ == "__main__":
    sys.exit(main())
