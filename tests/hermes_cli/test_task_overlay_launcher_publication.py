"""Shared source launchers survive cleanup of a borrowing task's home."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from hermes_cli import _launchers
from tests.hermes_cli.test_source_launcher_publication import fixture_tree, select_generation, _record_store_python


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('runtime_override', [False, True])
@pytest.mark.parametrize('stamped', [False, True])
@pytest.mark.parametrize('poisoned', [None, 'live', 'dead'])
def test_task_publication_repairs_launchers_using_install_python(tmp_path, monkeypatch, stamped, poisoned, runtime_override):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    select_generation(repo, 'owner', 'owner-ready')
    stable_python = _record_store_python(home / 'tools', interpreter)
    if stamped:
        (repo / 'install-stamp.json').write_text(json.dumps({'updateMechanism': 'self'}))
    local = repo / '.hermes' / 'bin'
    _launchers.ensure_install_launchers(repo, local)
    task = tmp_path / 'task-ephemeral' / 'hermes-home'
    task_store = task / 'tools'
    task_store.mkdir(parents=True)
    task_python = _record_store_python(task_store, interpreter)
    # A previous task already overwrote both shared commands with its own Python.
    if poisoned:
        for name in _launchers.ENTRY_POINTS:
            assert _launchers.mint_launcher(name, repo, local, task_python, None)
        if poisoned == 'dead':
            task_python.unlink()
    monkeypatch.setenv('HERMES_HOME', str(task))
    if runtime_override:
        monkeypatch.setenv('HERMES_RUNTIME_DIR', str(task_store))
    written = _launchers.ensure_install_launchers(repo, local)
    assert len(written) == len(_launchers.ENTRY_POINTS)
    # The client may still use task state while alive. Published commands must outlive it.
    shutil.rmtree(task.parent)
    env = dict(os.environ, HERMES_HOME=str(home))
    env.pop('HERMES_RUNTIME_DIR', None)
    for launcher in written:
        completed = subprocess.run([launcher], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
        assert completed.returncode == 7, completed.stderr
        receipt = json.loads(completed.stdout)
        assert receipt['value'] == 'owner-ready'
        assert Path(receipt['exe']) == stable_python


@pytest.mark.platforms('posix')
def test_task_python_cannot_replace_a_missing_owned_interpreter(tmp_path, monkeypatch):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    select_generation(repo, 'owner', 'owner-ready')
    (repo / 'install-stamp.json').write_text(json.dumps({'updateMechanism': 'self'}))
    (home / 'tools' / 'facts.json').unlink()
    task_store = tmp_path / 'task-ephemeral' / 'hermes-home' / 'tools'
    task_store.mkdir(parents=True)
    _record_store_python(task_store, interpreter)
    monkeypatch.setenv('HERMES_HOME', str(task_store.parent))
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(task_store))
    local = repo / '.hermes' / 'bin'
    assert _launchers.ensure_install_launchers(repo, local) == []
    assert not list(local.iterdir())
