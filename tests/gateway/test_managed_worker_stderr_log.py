"""Managed-worker stderr (its tracebacks and redirected prints) lands in the owning profile's
private, redacted, size-capped ``logs/managed-worker.log`` instead of being discarded."""
import asyncio
import io
import json
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef

SECRET = 'sk-proj-' + 'Q' * 40
CRASH = f"""import sys
sys.stderr.write('Traceback (most recent call last):\\n')
sys.stderr.write('RuntimeError: provider refused api_key={SECRET} Authorization: Bearer {SECRET}\\n')
sys.stderr.flush()
raise SystemExit(1)
"""


@pytest.mark.platforms("linux")
def test_worker_stderr_reaches_a_private_redacted_profile_log(tmp_path, monkeypatch):
    from gateway import session_managed_worker as managed
    original = subprocess.Popen

    def crashing_child(args, **kwargs):
        return original([sys.executable, '-c', CRASH], **kwargs)
    monkeypatch.setattr(managed.subprocess, 'Popen', crashing_child)
    monkeypatch.setattr(managed, '_worker_env', lambda authority: None)
    authority = SimpleNamespace(profile_id=str(tmp_path), pending_results={}, waiters={},
                                adopt_agent=lambda session_id, generation, worker: None)
    row = {'admission_id': 'adm', 'principal_id': 'owner', 'generation': 3, 'payload': {'text': 'go'}}
    with pytest.raises(EOFError):
        asyncio.run(asyncio.wait_for(managed.execute_managed(authority, SessionRef('p', 's'), row, None), 20))
    log = tmp_path / 'logs' / 'managed-worker.log'
    text = log.read_text()
    assert 'Traceback (most recent call last)' in text and 'RuntimeError: provider refused' in text
    assert SECRET not in text and 'QQQQQQQQQQ' not in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_worker_log_is_bounded_and_scrubs_the_workers_exact_secrets(tmp_path):
    from gateway.session_managed_worker_log import LOG_NAME, MAX_LOG_BYTES, drain_worker_stderr
    path = tmp_path / 'logs' / LOG_NAME
    assignment = 'opaque-assignment-secret-value'
    noise = (f'line {assignment} ' + 'x' * 200 + '\n') * 12_000 + 'y' * 300_000 + '\nlast\n'
    drain_worker_stderr(io.BytesIO(noise.encode()), path, 7, lambda: [assignment])
    files = [path.with_name(LOG_NAME + '.1'), path]  # oldest first
    assert all(f.stat().st_size <= MAX_LOG_BYTES for f in files)
    assert not list(path.parent.glob(LOG_NAME + '.2'))
    text = ''.join(f.read_text() for f in files)
    assert assignment not in text and '[truncated]' in text and text.endswith('[pid 7] last\n')


def test_bootstrap_client_secrets_are_redacted_from_worker_log(tmp_path):
    from gateway.session_managed_worker_log import bootstrap_log_secrets, drain_worker_stderr
    client = {'MULTICA_TOKEN': 'opaque-client-value', 'MULTICA_TASK_CONFIG_ROOT': '/private/task-config-location'}
    frame = {'api_key': 'opaque-model-key', 'scope': {'secret': 'opaque-assignment'}, 'policy': {
        'client_env_json': json.dumps(client), 'client_env_rules_json': json.dumps({'secret_names': list(client)})}}
    secrets = bootstrap_log_secrets(frame)
    assert set(secrets) == {*client.values(), frame['api_key'], frame['scope']['secret']}
    path = tmp_path / 'logs' / 'managed-worker.log'
    drain_worker_stderr(io.BytesIO(('RuntimeError: ' + ' '.join(secrets) + '\n').encode()), path, 7, lambda: secrets)
    text = path.read_text()
    assert 'RuntimeError:' in text
    assert all(value not in text for value in secrets)
    # Legacy and empty-client bootstrap frames still redact the worker's own keys.
    assert bootstrap_log_secrets({**frame, 'policy': {}}) == [frame['api_key'], frame['scope']['secret']]
