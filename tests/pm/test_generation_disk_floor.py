"""Low disk must stop new dependency generations without stopping their readers."""
from pathlib import Path
import os
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("total_gib", [100, 1000])
def test_generation_write_requires_live_disk_floor(tmp_path, monkeypatch, total_gib):
    from pm import runtime, runtime_stage

    total = total_gib * 1024**3
    floor = max(3 * 1024**3, (total + 99) // 100)
    stats = SimpleNamespace(f_frsize=1, f_blocks=total, f_bavail=floor - 1)
    monkeypatch.setattr(os, "statvfs", lambda path: stats)
    monkeypatch.setattr(runtime, "_inputs", lambda *args, **kwargs: "inputs")
    monkeypatch.setattr(runtime, "_hold_for_children", lambda path: None)
    writes = []

    def stage(uv, python, destination, **kwargs):
        writes.append(destination)
        destination.mkdir(parents=True)
        return destination / "python"

    monkeypatch.setattr(runtime_stage, "stage_runtime", stage)
    root = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="disk floor"):
        runtime.prepare_runtime(Path("uv"), Path(sys.executable), root)
    assert writes == []
    assert not (root / "generations").exists()

    # Read the volume again: exactly the floor is permitted after space recovers.
    stats.f_bavail = floor
    runtime.prepare_runtime(Path("uv"), Path(sys.executable), root)
    assert len(writes) == 1


@pytest.mark.platforms("posix")
def test_lease_write_remains_allowed_below_disk_floor(tmp_path, monkeypatch):
    from hermes_cli.runtime_state import lease_directory, leases_held

    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".lease-managed").touch()
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(
        f_frsize=4096, f_blocks=1024**3, f_bavail=1))

    release = lease_directory(generation)
    try:
        assert list((generation / ".leases").iterdir())
        assert leases_held(generation)
    finally:
        release()
