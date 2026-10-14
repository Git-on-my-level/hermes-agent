"""Serving selects completed code without update I/O or dependency probing."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import serving_generation as sg
from pm.environments import install_state_dir, runtime_facts_path, venv_python


def installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    root = tmp_path / "checkout"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "hermes_cli" / "__init__.py").touch()
    (root / "hermes_cli" / "main.py").write_text("print('previous good code')\n")
    (root / "pyproject.toml").write_text('[project]\nname="test"\n[tool.setuptools.packages.find]\ninclude=["hermes_cli*", "hermes_constants*", "pm*"]\n')
    generation = install_state_dir(root) / "environments" / "completed"
    python = venv_python(generation / "venv")
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (generation / "venv" / "pyvenv.cfg").write_text("home = test\n")
    runtime_facts_path(root).write_text(json.dumps({"packages": {"venv": {"environment": str(generation / "venv")}}}))
    return root, generation


def test_failed_update_boots_old_code_without_mutable_checkout(tmp_path, monkeypatch):
    root, generation = installed(tmp_path, monkeypatch)
    assert sg.record_completed_generation(root, {"commit": "abc123", "dirty": False})
    # Child command uses the snapshot, even after an interrupted checkout swap.
    (root / "hermes_cli" / "main.py").write_text("raise RuntimeError('broken target')\n")
    command = sg.serving_command(root, [str(root / "hermes"), "gateway", "run"])
    assert command and command[0] == str(venv_python(generation / "venv"))
    # Supply only the existing generic lease implementation; no application import
    # or update preparation is needed before dispatching the frozen entry point.
    snapshot = Path(sg.read_generation(root)["source"])
    (snapshot / "hermes_cli" / "runtime_state.py").write_text("def lease_generation(x): pass\ndef lease_directory(x): pass\n")
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "previous good code"


def test_completion_only_publishes_good_pairs(tmp_path, monkeypatch):
    root, generation = installed(tmp_path, monkeypatch)
    assert sg.record_completed_generation(root, {"commit": "good", "dirty": False})
    before = sg.record_path(root).read_bytes()
    for stamp in ({"commit": "bad", "dirty": True}, {"commit": "adopted", "dirty": False, "adoptedAt": "now"}):
        assert not sg.record_completed_generation(root, stamp)
        assert sg.record_path(root).read_bytes() == before
    record = json.loads(before)
    record["generation"] = str(tmp_path / "foreign")
    sg.record_path(root).write_text(json.dumps(record))
    assert sg.serving_command(root, ["hermes", "gateway", "run"]) is None


def test_legacy_serving_boot_never_calls_update_worker(tmp_path, monkeypatch):
    from hermes_cli import venv_sync, steward, _launchers
    import pm
    root, generation = installed(tmp_path, monkeypatch)
    (root / ".git").mkdir()
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(steward, "read_install_stamp", lambda r: {"updateMechanism": "self"})
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda r: venv_python(generation / "venv"))
    monkeypatch.setattr(pm, "venv_is_current", lambda **k: pytest.fail("boot waited for PM worker"))
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: pytest.fail("boot finished update"))
    assert venv_sync.prepare_launch(root, ["gateway", "run"]) == venv_python(generation / "venv")


def test_gc_keeps_completed_pair_and_leased_superseded_source(tmp_path, monkeypatch):
    from hermes_cli.runtime_state import collect_generations, lease_directory
    root, generation = installed(tmp_path, monkeypatch)
    (generation / ".lease-managed").touch()
    assert sg.record_completed_generation(root, {"commit": "first", "dirty": False})
    first = Path(sg.read_generation(root)["source"])
    release = lease_directory(first)
    try:
        assert sg.record_completed_generation(root, {"commit": "second", "dirty": False})
        collect_generations(root, min_age_seconds=0)
        assert first.is_dir() and generation.is_dir()
    finally:
        release()
    removed = collect_generations(root, min_age_seconds=0)
    assert first in removed and not first.exists()
    assert Path(sg.read_generation(root)["source"]).is_dir()


def test_direct_gateway_and_profile_cli_use_completed_pair(tmp_path, monkeypatch):
    root, _ = installed(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    sg.record_completed_generation(root, {"commit": "good", "dirty": False})
    direct = sg.serving_command(root, [str(root / "gateway" / "run.py")])
    assert direct and "gateway.run" in direct[-1]
    profile = sg.serving_command(root, ["hermes", "--profile", "other", "gateway", "run"])
    assert profile and "'--profile', 'other'" in profile[-1]
    assert sg.serving_command(root, ["hermes", "update"]) is None
