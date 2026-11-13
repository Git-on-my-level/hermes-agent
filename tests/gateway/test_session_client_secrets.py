"""Explicit client credentials remain local to the owning session's child processes."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from tests.gateway.test_session_client_env import _CONFIG, _create, _home, _owner


@pytest.mark.asyncio
async def test_concurrent_sessions_only_spawn_their_own_client_secrets(tmp_path, monkeypatch, caplog):
    from gateway.session_local import local_session_info
    from gateway.session_policy import policy_scope
    from tools.code_execution_env import _build_child_env
    from tools.environments.local import _make_run_env
    home = _home(tmp_path, _CONFIG + 'terminal:\n  client_env_passthrough_secrets: [MULTICA_TOKEN, MULTICA_TASK_CONFIG_ROOT]\n')
    monkeypatch.setenv('MULTICA_TOKEN', 'gateway-must-not-leak')
    monkeypatch.setenv('MULTICA_TASK_CONFIG_ROOT', '/gateway-must-not-leak')
    owner, connection = await _owner(home, monkeypatch)
    from tools import env_passthrough
    monkeypatch.setattr(env_passthrough, 'scoped_passthrough_additions',
                        lambda present: {'MULTICA_TOKEN': 'profile-must-not-leak'})
    before = dict(os.environ)
    names = ('MULTICA_TOKEN', 'MULTICA_TASK_CONFIG_ROOT')
    ready = asyncio.Event()
    arrived = []

    async def spawn(policy):
        with policy_scope(policy, authority=owner):
            arrived.append(True)
            if len(arrived) == 3:
                ready.set()
            await ready.wait()  # all three scopes are active before any child starts
            environments = [_make_run_env({}), _build_child_env(rpc_endpoint='unused', rpc_token='unused',
                            tmpdir=str(tmp_path), child_python=sys.executable)]
            results = []
            for env in environments:
                completed = await asyncio.to_thread(subprocess.run, [sys.executable, '-c',
                    'import os,json; print(json.dumps({k:os.environ.get(k) for k in ' + repr(names) + '}))'],
                    env=env, capture_output=True, text=True, check=True)
                results.append(json.loads(completed.stdout))
            return results
    try:
        policies = []
        expected = []
        for request, value in [('first', 'first-private-value'), ('second', 'second-private-value'), ('empty', None)]:
            env = {} if value is None else {'MULTICA_TOKEN': value, 'MULTICA_TASK_CONFIG_ROOT': '/' + request}
            ref, policy = _create(owner, connection.actor, home, request, client_env=env)
            policies.append(policy)
            expected.append({name: env.get(name) for name in names})
            public = json.dumps(local_session_info(owner, ref))
            if value:
                assert value not in public
                assert value not in policy.request_json
        results = await asyncio.wait_for(asyncio.gather(*(spawn(p) for p in policies)), 30)
        assert results == [[e, e] for e in expected]
        assert dict(os.environ) == before
        assert 'first-private-value' not in caplog.text
        assert 'second-private-value' not in caplog.text
    finally:
        await connection.close()
        owner.db.close()


def test_client_secret_opt_in_is_exact_local_and_cannot_forward_hermes_credentials():
    from agent.session_client_env import accept_client_env, client_env_rules, client_env_scope, collect_client_env, toolchain_environ
    names = ['MULTICA_TOKEN', 'MULTICA_TASK_CONFIG_ROOT', 'OPENAI_API_KEY', 'HERMES_YOLO_MODE',
             '_HERMES_FORCE_OPENAI_API_KEY', 'MULTICA_*']
    config = {'terminal': {'client_env_passthrough_secrets': names}}
    sent = {name: 'private' for name in names}
    expected = {name: 'private' for name in names[:2]}
    assert collect_client_env(sent, config) == expected
    assert accept_client_env(sent, config) == expected
    assert collect_client_env(sent) == {}
    assert accept_client_env(sent, {}) == {}
    assert accept_client_env(sent, config, local=False) == {}
    rules = client_env_rules(config)
    with client_env_scope(expected, local=False, rules=rules):
        assert 'MULTICA_TOKEN' not in toolchain_environ({})
    with client_env_scope({}, rules=rules):
        assert 'MULTICA_TOKEN' not in toolchain_environ({'MULTICA_TOKEN': 'gateway'})
    # Old frozen sessions do not acquire a new opt-in after a config change.
    with client_env_scope(sent, rules=client_env_rules({})):
        assert 'MULTICA_TOKEN' not in toolchain_environ({})
