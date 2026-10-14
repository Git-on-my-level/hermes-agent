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
        data = json.loads(record_path(root).read_text(encoding="utf-8-sig"))
        generation = Path(data["generation"]).resolve()
        if generation.parent != (install_state_dir(root) / "environments").resolve():
            return None
        if not venv_python(generation / "venv").is_file():
            return None
        source = Path(data["source"]).resolve()
        if source.parent != (install_state_dir(root) / "serving-source").resolve():
            return None
        if not _complete_source(source, data['commit']):
            return None
        if not _dependency_inputs_match(generation / 'workspace', source):
            return None
        if not isinstance(data["commit"], str) or not data["commit"]:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _complete_source(source: Path, commit: str) -> bool:
    try:
        if not (source / 'hermes_cli' / 'main.py').is_file():
            return False
        if any(not (source / name).is_dir() or (source / name).is_symlink()
               for name in ('skills', 'locales', 'scripts')):
            return False
        stamp = json.loads((source / 'install-stamp.json').read_text(encoding='utf-8-sig'))
        return stamp.get('commit') == commit and stamp.get('dirty') is False
    except (OSError, ValueError, AttributeError):
        return False


def _dependency_inputs_match(workspace: Path, source: Path) -> bool:
    """PM can reuse code-independent dependencies; their declared inputs must agree."""
    import tomllib
    try:
        documents = [tomllib.loads((path / 'pyproject.toml').read_text(encoding='utf-8-sig'))
                     for path in (workspace, source)]
        return (documents[0].get('project') == documents[1].get('project')
                and documents[0].get('build-system') == documents[1].get('build-system'))
    except (OSError, ValueError):
        return False


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
    from hermes_cli.version_info import _git_version_info, _run_git
    info = _git_version_info(Path(root), include_untracked=True)
    commit = _run_git(Path(root), 'rev-parse', str(stamp['commit']))
    if info.dirty or not commit or info.commit != commit:
        return False
    import shutil
    import tempfile
    from scripts.bundles.payload import snapshot as snapshot_source, INERT_SNAPSHOT_DIRS
    snapshots = install_state_dir(root) / "serving-source"
    snapshots.mkdir(parents=True, exist_ok=True)
    snapshot = snapshots / stamp["commit"]
    if not snapshot.is_dir():
        staging = Path(tempfile.mkdtemp(dir=snapshots, prefix=".staging-"))
        try:
            # Reuse the distribution snapshot contract, retaining runtime bridge
            # scripts. This also freezes locales, catalog manifests and bundled
            # skills that are outside setuptools' Python-package roots.
            excluded = tuple(name for name in INERT_SNAPSHOT_DIRS if name != "scripts")
            snapshot_source(Path(root), stamp["commit"], staging, exclude=excluded)
            atomic_json_write(staging / "install-stamp.json", {**stamp, "updateMechanism": "external"})
            (staging / ".lease-managed").touch()
            os.replace(staging, snapshot)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    if not _complete_source(snapshot, stamp['commit']) or not _dependency_inputs_match(generation / 'workspace', snapshot):
        return False
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
    # Site processing (-I included) can preload core modules or install an
    # editable finder ahead of PathFinder. Fence every core import to this full
    # source tree; plugin/dependency finders remain available for other names.
    roots = sorted({p.stem for p in snapshot.glob('*.py')} |
                   {p.name for p in snapshot.iterdir() if p.is_dir() and (p / '__init__.py').is_file()})
    code = f"""import sys, os, runpy
from importlib.machinery import PathFinder
snapshot = {str(snapshot)!r}
roots = set({roots!r})
for name in tuple(sys.modules):
    if name.partition('.')[0] in roots:
        del sys.modules[name]
class ServingSourceFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname.partition('.')[0] not in roots:
            return None
        spec = PathFinder.find_spec(fullname, [snapshot] if path is None else path)
        if spec is None:
            raise ImportError('Completed serving source is missing ' + fullname)
        if spec.origin and spec.origin not in ('built-in', 'frozen'):
            from pathlib import Path
            if not Path(spec.origin).resolve().is_relative_to(Path(snapshot)):
                raise ImportError('Core module escapes completed source: ' + fullname)
        return spec
sys.meta_path.insert(0, ServingSourceFinder)
sys.path.insert(0, snapshot)
os.environ['HERMES_INSTALL_ROOT'] = snapshot
from pathlib import Path
from hermes_cli.runtime_state import lease_generation, lease_directory
lease_generation(Path({str(generation / 'venv')!r}))
lease_directory(Path(snapshot))
sys.argv = {argv!r}
runpy.run_module({module!r}, run_name='__main__')
"""
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
