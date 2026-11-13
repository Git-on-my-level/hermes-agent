"""ACP session creation forwards the launching client's explicitly allowed environment."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_new_acp_session_sends_client_env_using_profile_config(tmp_path, monkeypatch):
    from acp_adapter.gateway_server import GatewayACPAgent
    (tmp_path / 'config.yaml').write_text('terminal:\n  client_env_passthrough_secrets: [MULTICA_TOKEN]\n')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MULTICA_TOKEN', 'acp-client-private')
    monkeypatch.setenv('UNLISTED_TOKEN', 'unlisted-private')
    agent = GatewayACPAgent()
    agent._client = AsyncMock()
    client = agent._client.return_value
    client.rpc.side_effect = [
        {'session_create': {'sources': ['acp'], 'parameters': ['client_env']},
         'capabilities': ['acp-editor-policy-v1']},
        {'session_id': 's', 'info': {'model': 'test'}},
    ]
    agent._adopt = AsyncMock()
    agent._request_live_reasoning = AsyncMock()
    agent._session_fields = AsyncMock(return_value={})
    await agent.new_session(str(tmp_path))
    call = client.rpc.await_args_list[1]
    assert call.args == ('session.create',)
    assert call.kwargs['client_env']['MULTICA_TOKEN'] == 'acp-client-private'
    assert 'UNLISTED_TOKEN' not in call.kwargs['client_env']
