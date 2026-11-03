"""Disk admission for dependency/source writers, never runtime-state writers.

Stdlib-only: PM also runs independently, before Hermes is importable.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from pm.package import InstallError


class DiskFloorError(InstallError):
    """A dependency write would consume the volume's serving reserve."""


def require_generation_space(*destinations: Path) -> None:
    """Check each destination's live volume, including not-yet-created paths."""
    for destination in destinations:
        probe = Path(destination).resolve()
        while not probe.exists():
            probe = probe.parent
        if hasattr(os, "statvfs"):
            stats = os.statvfs(probe)
            total = stats.f_blocks * stats.f_frsize
            free = stats.f_bavail * stats.f_frsize
        else:
            # Windows has no statvfs; disk_usage uses the native volume API.
            total, _, free = shutil.disk_usage(probe)
        floor = max(3 * 1024**3, (total + 99) // 100)
        if free < floor:
            raise DiskFloorError(
                "disk", f"disk floor at {probe}: {free} bytes available, {floor} required",
                "free space before retrying dependency or source generation writes",
            )
