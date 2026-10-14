"""Pin a completed PM source/dependency pair independently of a mutable checkout.

A serving restart never performs update work. The update command owns repair
and advances this record only after its completion tail has succeeded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def record_path(root: Path) -> Path:
    from pm.environments import install_state_dir
    return install_state_dir(root) / "serving-generation.json"


def read_generation(root: Path) -> dict | None:
    from pm.environments import install_state_dir, venv_python
    try:
        data = json.loads(record_path(root).read_text(encoding="utf-8"))
        generation = Path(data["generation"]).resolve()
        if generation.parent != (install_state_dir(root) / "environments").resolve():
            return None
        if not venv_python(generation / "venv").is_file():
            return None
        source = Path(data["source"]).resolve()
        if source.parent != (install_state_dir(root) / "serving-source").resolve():
            return None
        if not (source / "hermes_cli" / "main.py").is_file():
            return None
        if not isinstance(data["commit"], str) or not data["commit"]:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_completed_generation(root: Path, stamp: dict) -> bool:
    """Publish source at update completion; no source copy or install at boot."""
    from pm.environments import committed_venv, install_state_dir
    from utils import atomic_json_write
    if stamp.get("dirty") is not False or stamp.get("adoptedAt") or not stamp.get("commit"):
        return False
    environment = committed_venv(root)
    if environment is None:
        return False
    generation = environment.parent.resolve()
    import shutil
    import tempfile
    from pm.workspace import _copy_core_inputs
    snapshots = install_state_dir(root) / "serving-source"
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot = snapshots / stamp["commit"]
    if not snapshot.is_dir():
        staging = Path(tempfile.mkdtemp(dir=snapshots, prefix=".staging-"))
        try:
            _copy_core_inputs(Path(root), staging)
            if (Path(root) / "uv.lock").is_file():
                shutil.copy2(Path(root) / "uv.lock", staging / "uv.lock")
            atomic_json_write(staging / "install-stamp.json", {**stamp, "updateMechanism": "external"})
            (staging / ".lease-managed").touch()
            os.replace(staging, snapshot)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    atomic_json_write(record_path(root), {"schema": 1, "generation": str(generation),
                                        "source": str(snapshot), "commit": stamp["commit"]})
    return True


def serving_command(root: Path, argv: list[str]) -> list[str] | None:
    from hermes_cli.venv_sync import _is_serving_gateway
    from pm.environments import venv_python
    direct = Path(argv[0]).resolve() == root / "gateway" / "run.py"
    if not direct and not _is_serving_gateway(argv[1:]):
        return None
    record = read_generation(root)
    if record is None:
        return None
    generation = Path(record["generation"])
    snapshot = Path(record["source"])
    # Direct gateway.run and durable `hermes gateway run` both keep their CLI
    # grammar. External console launchers must dispatch inside the snapshot.
    module = "gateway.run" if direct else "hermes_cli.main"
    code = (f"import sys, runpy, os; sys.path.insert(0, {str(snapshot)!r}); "
            f"os.environ['HERMES_INSTALL_ROOT'] = {str(snapshot)!r}; "
            "from pathlib import Path; from hermes_cli.runtime_state import lease_generation, lease_directory; "
            f"lease_generation(Path({str(generation / 'venv')!r})); lease_directory(Path({str(snapshot)!r})); "
            f"sys.argv = {argv!r}; runpy.run_module({module!r}, run_name='__main__')")
    return [str(venv_python(generation / "venv")), "-I", "-c", code]


def launch_completed_gateway(root: Path) -> None:
    command = serving_command(root, sys.argv)
    if command is None:
        return
    print("hermes: serving completed source/dependency generation; unfinished updates remain owed "
          "to `hermes update` / the next idle converge window", file=sys.stderr, flush=True)
    if os.name == "nt":
        import subprocess
        raise SystemExit(subprocess.call(command))
    os.execv(command[0], command)
