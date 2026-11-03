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
    (root / "hermes_cli" / "runtime_state.py").write_text('def lease_generation(x): pass\ndef lease_directory(x): pass\n', encoding='utf-8')
    (root / "hermes_cli" / "source_completion.py").touch()
    (root / "hermes_cli" / "main.py").write_text("print('previous good code')\n", encoding='utf-8')
    (root / "pyproject.toml").write_text('[project]\nname="test"\n[tool.setuptools.packages.find]\ninclude=["hermes_cli*", "hermes_constants*", "pm*"]\n', encoding='utf-8')
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "locales").mkdir()
    (root / "locales" / "en.yaml").write_text("hello: preserved\n", encoding='utf-8')
    (root / "scripts").mkdir()
    (root / "scripts" / "bridge.js").write_text("// preserved runtime bridge\n", encoding='utf-8')
    (root / 'skills').mkdir()
    (root / 'skills' / 'fixture.txt').write_text('preserved skill', encoding='utf-8')
    (root / '.gitignore').write_text('install-stamp.json\n', encoding='utf-8')
    git_env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    def git(*args):
        subprocess.run(["git", *args], cwd=root, env=git_env, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.name", "Hermes Test")
    git("config", "user.email", "test@example.invalid")
    git("add", ".")
    git("commit", "-qm", "completed source")
    for tag in ("abc123", "good", "first", "second"):
        git("tag", tag)
    generation = install_state_dir(root) / "environments" / "completed"
    python = venv_python(generation / "venv")
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (generation / "venv" / "pyvenv.cfg").write_text("home = test\n", encoding='utf-8')
    from pm.packages import Venv
    runtime_facts_path(root).write_text(json.dumps({"schema":1, "packages": {"venv": {
        "environment": str(generation / "venv"), "extras": [], "stamp": Venv(root).expected_stamp([], plugin_dirs=[]),
    }}}), encoding='utf-8')
    from pm.workspace import _copy_core_inputs
    workspace = generation / 'workspace'
    workspace.mkdir()
    _copy_core_inputs(root, workspace)
    return root, generation


def test_failed_update_boots_old_code_without_mutable_checkout(tmp_path, monkeypatch):
    root, generation = installed(tmp_path, monkeypatch)
    assert sg.record_completed_generation(root, {"commit": "abc123", "dirty": False})
    # Child command uses the snapshot, even after an interrupted checkout swap.
    (root / "hermes_cli" / "main.py").write_text("raise RuntimeError('broken target')\n", encoding='utf-8')
    command = sg.serving_command(root, [str(root / "hermes"), "gateway", "run"])
    assert command and command[0] == str(venv_python(generation / "venv"))
    # Supply only the existing generic lease implementation; no application import
    # or update preparation is needed before dispatching the frozen entry point.
    snapshot = Path(sg.read_generation(root)["source"])
    assert (snapshot / "locales" / "en.yaml").is_file()
    assert (snapshot / "scripts" / "bridge.js").is_file()
    (snapshot / "hermes_cli" / "runtime_state.py").write_text("def lease_generation(x): pass\ndef lease_directory(x): pass\n", encoding='utf-8')
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, encoding='utf-8', check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "previous good code"


def test_completion_only_publishes_good_pairs(tmp_path, monkeypatch):
    root, generation = installed(tmp_path, monkeypatch)
    assert sg.record_completed_generation(root, {"commit": "good", "dirty": False})
    before = sg.record_path(root).read_bytes()
    sg.record_path(root).write_bytes(b"\xef\xbb\xbf" + before)
    assert sg.read_generation(root) is not None
    sg.record_path(root).write_bytes(before)
    for stamp in ({"commit": "bad", "dirty": True}, {"commit": "adopted", "dirty": False, "adoptedAt": "now"}):
        assert not sg.record_completed_generation(root, stamp)
        assert sg.record_path(root).read_bytes() == before
    workspace_project = generation/'workspace'/'pyproject.toml'
    original_project = workspace_project.read_text(encoding='utf-8-sig')
    workspace_project.write_text('[project]\nname="mismatched-dependency-inputs"\n', encoding='utf-8')
    assert sg.record_completed_generation(root, {'commit':'good', 'dirty':False}) is False
    assert sg.record_path(root).read_bytes() == before
    workspace_project.write_text(original_project, encoding='utf-8')
    record = json.loads(before)
    record["generation"] = str(tmp_path / "foreign")
    sg.record_path(root).write_text(json.dumps(record), encoding='utf-8')
    assert sg.serving_command(root, ["hermes", "gateway", "run"]) is None


def test_legacy_serving_boot_never_calls_update_worker(tmp_path, monkeypatch):
    from hermes_cli import venv_sync, steward, _launchers
    import pm
    root, generation = installed(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(steward, "read_install_stamp", lambda r: {"updateMechanism": "self"})
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda r: venv_python(generation / "venv"))
    monkeypatch.setattr(pm, "venv_is_current", lambda **k: pytest.fail("boot waited for PM worker"))
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **k: pytest.fail("boot finished update"))
    with pytest.raises(RuntimeError, match='Refusing gateway startup'):
        venv_sync.prepare_launch(root, ['gateway', 'run'])


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


def test_gc_keeps_legacy_source_whose_venv_symlink_still_serves(tmp_path, monkeypatch):
    """Pre-2026-10-18 snapshots carry ``venv -> environments/<gen>/venv``; a process that
    still resolves its interpreter through one (un-updated host, stale cache) holds no lease
    file, so the collector must treat the link itself as held — removing the source would
    strand that process's cron scripts on a missing interpreter. A dangling link (its target
    generation already collected) pins nothing."""
    from hermes_cli.runtime_state import collect_generations
    root, generation = installed(tmp_path, monkeypatch)
    (generation / ".lease-managed").touch()
    assert sg.record_completed_generation(root, {"commit": "first", "dirty": False})
    first = Path(sg.read_generation(root)["source"])
    target = tmp_path / "elsewhere-venv"
    target.mkdir()
    (first / "venv").symlink_to(target, target_is_directory=True)
    assert sg.record_completed_generation(root, {"commit": "second", "dirty": False})
    removed = collect_generations(root, min_age_seconds=0)
    assert first not in removed and first.is_dir()
    # The guard is the link's liveness, not its name: a dangling link no longer serves any
    # process's interpreter resolution, so the source is collected normally.
    target.rmdir()
    removed = collect_generations(root, min_age_seconds=0)
    assert first in removed and not first.exists()


def test_direct_gateway_and_profile_cli_use_completed_pair(tmp_path, monkeypatch):
    root, _ = installed(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    sg.record_completed_generation(root, {"commit": "good", "dirty": False})
    direct = sg.serving_command(root, [str(root / "gateway" / "run.py")])
    assert direct and "gateway.run" in direct[-1]
    profile = sg.serving_command(root, ["hermes", "--profile", "other", "gateway", "run"])
    assert profile and "'--profile', 'other'" in profile[-1]
    assert sg.serving_command(root, ["hermes", "update"]) is None


def test_serving_fences_preloaded_editable_finder_and_validates_resources(tmp_path, monkeypatch):
    import shutil
    import sysconfig
    from pm.workspace import _copy_core_inputs
    root, generation = installed(tmp_path, monkeypatch)
    (root / 'pm').mkdir()
    (root / 'pm' / '__init__.py').touch()
    (root / 'pm' / 'paths.py').write_text("import os\nfrom pathlib import Path\ndef install_root(): return Path(os.environ['HERMES_INSTALL_ROOT'])\n", encoding='utf-8')
    (root / 'hermes_cli' / 'runtime_state.py').write_text('def lease_generation(x): pass\ndef lease_directory(x): pass\n', encoding='utf-8')
    (root / 'hermes_cli' / 'main.py').write_text("""import json
from pathlib import Path
import hermes_cli
from pm.paths import install_root
root = Path(__file__).resolve().parents[1]
print(json.dumps([str(root), str(install_root()), str(Path(hermes_cli.__file__).resolve().parents[1]),
 (root/'skills'/'fixture.txt').read_text(), (root/'locales'/'en.yaml').read_text(), (root/'scripts'/'bridge.js').read_text()]))
""", encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-qm', 'resource reader'], cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, encoding='utf-8').strip()
    shutil.rmtree(generation / 'workspace')
    _copy_core_inputs(root, generation / 'workspace')
    # A real venv/site startup processes this deliberately high-priority editable
    # finder, preloading the package-only workspace before the serving bootstrap.
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(generation/'venv')], check=True, capture_output=True)
    python = venv_python(generation/'venv')
    site = Path(subprocess.check_output([str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True, encoding='utf-8').strip())
    site.mkdir(parents=True, exist_ok=True)
    (site/'poison_finder.py').write_text(f"""import sys
from importlib.machinery import PathFinder
class Finder:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name.partition('.')[0] in ('hermes_cli', 'pm'):
            return PathFinder.find_spec(name, [{str(generation/'workspace')!r}] if path is None else path)
sys.meta_path.insert(0, Finder)
import hermes_cli
""", encoding='utf-8')
    (site/'editable.pth').write_text('import poison_finder\n', encoding='utf-8')
    assert sg.record_completed_generation(root, {'commit': commit, 'dirty': False})
    source = Path(sg.read_generation(root)['source'])
    result = subprocess.run(sg.serving_command(root, ['hermes', 'gateway', 'run']), capture_output=True, text=True, encoding='utf-8', timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values[:3] == [str(source)] * 3
    assert values[3:] == ['preserved skill', 'hello: preserved\n', '// preserved runtime bridge\n']
    shutil.rmtree(source/'skills')
    assert sg.read_generation(root) is None


@pytest.mark.parametrize('fault', ['checkout_moved', 'new_pending', 'missing_stamp_pending'])
@pytest.mark.parametrize('disable_lazy', [False, True])
def test_unrecorded_generation_refuses_half_updated_tree(tmp_path, monkeypatch, fault, disable_lazy, capsys):
    from hermes_cli import venv_sync, _launchers
    root, _ = installed(tmp_path, monkeypatch)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, encoding='utf-8').strip()
    (root/'install-stamp.json').write_text(json.dumps({'updateMechanism':'self','commit':commit,'dirty':False}), encoding='utf-8')
    if fault == 'checkout_moved':
        (root/'hermes_cli'/'main.py').write_text("raise RuntimeError('half-update')\n", encoding='utf-8')
        subprocess.run(['git','add','.'],cwd=root,check=True,capture_output=True)
        subprocess.run(['git','commit','-qm','unfinished target'],cwd=root,check=True,capture_output=True)
    else:
        venv_sync.completion_pending_path(root).parent.mkdir(parents=True, exist_ok=True)
        venv_sync.completion_pending_path(root).write_text('unfinished', encoding='utf-8')
        if fault == 'missing_stamp_pending':
            (root/'install-stamp.json').unlink()
    if disable_lazy:
        monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    else:
        monkeypatch.delenv('HERMES_DISABLE_LAZY_INSTALLS', raising=False)
    monkeypatch.setattr(_launchers, 'resolve_store_python', lambda r: pytest.fail('unsafe interpreter selected'))
    with pytest.raises(sg.GatewayServingRefused, match='Refusing gateway startup') as caught:
        venv_sync.prepare_launch(root, ['gateway','run'])
    assert 'half-updated tree' in str(caught.value)


@pytest.mark.parametrize('fault', ['consistent', 'consistent_adopted', 'pending', 'head_moved', 'dependency_mismatch'])
def test_first_deploy_at_real_bootstrap_process_boundary(tmp_path, monkeypatch, fault):
    root, _ = installed(tmp_path, monkeypatch)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, encoding='utf-8').strip()
    (root/'install-stamp.json').write_text(json.dumps({'updateMechanism':'self', 'commit':commit, 'dirty':False}), encoding='utf-8')
    if fault == 'consistent_adopted':
        stamp = json.loads((root/'install-stamp.json').read_text(encoding='utf-8-sig'))
        stamp['adoptedAt'] = 'legacy adoption'
        (root/'install-stamp.json').write_text(json.dumps(stamp), encoding='utf-8')
    if fault == 'pending':
        from hermes_cli.venv_sync import completion_pending_path
        completion_pending_path(root).write_text('unfinished', encoding='utf-8')
    elif fault == 'head_moved':
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'half-update'], cwd=root, check=True, capture_output=True)
    elif fault == 'dependency_mismatch':
        facts = json.loads(runtime_facts_path(root).read_text(encoding='utf-8-sig'))
        facts['packages']['venv']['stamp'] = 'half-updated dependencies'
        runtime_facts_path(root).write_text(json.dumps(facts), encoding='utf-8')
    # Import the REAL bootstrap and only redirect its install to the sandbox.
    # Activation is a tripwire: the old exception fallback must never reach it.
    code = f"""import sys, time
from pathlib import Path
from hermes_cli import serving_generation as sg, venv_sync
from pm import environments
root = Path({str(root)!r})
prepare, launch = venv_sync.prepare_launch, sg.launch_completed_gateway
venv_sync.prepare_launch = lambda ignored, argv: prepare(root, argv)
sg.launch_completed_gateway = lambda ignored: launch(root)
def unsafe(*args):
    print('UNSAFE MUTABLE CHECKOUT', file=sys.stderr)
    raise AssertionError('activation after refusal')
environments.activate_dependencies = unsafe
# A controlled clock exercises the production backoff at the process boundary.
# Record the full requested interval instead of sleeping for a minute in tests.
real_sleep = time.sleep
def controlled_sleep(seconds):
    if seconds == 60:
        (root/'backoff.json').write_text(str(seconds), encoding='utf-8')
    else:
        real_sleep(seconds)
time.sleep = controlled_sleep
sys.argv = ['hermes', 'gateway', 'run']
import hermes_bootstrap
print('UNSAFE BOOTSTRAP RETURNED')
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8', timeout=40, check=False)
    assert 'UNSAFE' not in result.stdout + result.stderr
    if fault in {'consistent', 'consistent_adopted'}:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'previous good code'
        assert 'first-deploy serving generation published from consistent install' in result.stderr
        assert 'serving completed source/dependency generation' in result.stderr
        assert sg.read_generation(root) is not None
        assert not (root/'backoff.json').exists()
    else:
        assert result.returncode == 78, result.stderr
        assert len(result.stderr.strip().splitlines()) == 1, result.stderr
        assert 'Refusing gateway startup' in result.stderr
        assert (root/'backoff.json').read_text(encoding='utf-8-sig') == '60'
        status = json.loads((tmp_path/'home'/'gateway_state.json').read_text(encoding='utf-8-sig'))
        assert status['gateway_state'] == 'startup_failed'
        assert status['platforms']['startup']['needs_attention'] is True
        assert not sg.record_path(root).exists()


@pytest.mark.parametrize('fault', ['dirty', 'unknown_git', 'dependency_mismatch', 'missing_resources', 'publish_false'])
def test_update_failed_publish_exits_nonzero_with_reason(tmp_path, monkeypatch, fault):
    root, generation = installed(tmp_path, monkeypatch)
    if fault == 'dirty':
        (root/'hermes_cli'/'main.py').write_text('uncommitted change', encoding='utf-8')
    elif fault == 'dependency_mismatch':
        (generation/'workspace'/'pyproject.toml').write_text('[project]\nname="incompatible"\n', encoding='utf-8')
    elif fault == 'missing_resources':
        import shutil
        shutil.rmtree(root/'skills')
        subprocess.run(['git','add','.'], cwd=root, check=True, capture_output=True)
        subprocess.run(['git','commit','-qm','missing skills'], cwd=root, check=True, capture_output=True)
    code = f"""import sys
from pathlib import Path
from hermes_cli import source_completion, venv_sync, source_build, update_cmd_maint, serving_generation
from pm import environments
environments.activate_dependencies = lambda root: None
venv_sync.publish_launchers = lambda root: None
source_build.build_update_products = lambda root, **kw: None
update_cmd_maint._run_post_update_maintenance = lambda **kw: True
if {fault!r} == 'publish_false':
    serving_generation.record_completed_generation = lambda *args: False
if {fault!r} == 'unknown_git':
    from hermes_cli import version_info
    from types import SimpleNamespace
    version_info._git_version_info = lambda *args, **kwargs: SimpleNamespace(commit=None)
# Exercise the real completion owner, including --no-gateway-restart advice.
from hermes_cli import update_completion, update_cmd
update_cmd._sweep_bytecode_after_update = lambda *args: None
update_completion._complete_selected({{'source':{str(root)!r}, 'sibling_snapshots':[], 'plan':None,
 'branch':'prod', 'desktop':False, 'assume_yes':True, 'gateway_mode':False,
 'snapshot_id':None, 'pre_update_version':None, 'no_gateway_restart':True}})
raise SystemExit(0)
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8', timeout=40, check=False)
    assert result.returncode != 0, result.stdout + result.stderr
    reasons = {'dirty':'dirty', 'unknown_git':'Git identity is unknown', 'dependency_mismatch':'dependency declarations differ',
               'missing_resources':'skills/locales/scripts', 'publish_false':'snapshot validation'}
    assert 'Serving generation publication failed' in result.stderr
    assert reasons[fault] in result.stderr
    assert 'Code updated!' not in result.stdout
    assert 'restart gateways separately' not in result.stdout
    assert sg.read_generation(root) is None
