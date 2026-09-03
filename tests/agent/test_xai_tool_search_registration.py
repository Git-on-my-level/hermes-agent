"""Provider-level registration tests for the Tool Search bridge."""

from run_agent import AIAgent


def _capture_tool_registration(monkeypatch, **agent_kwargs):
    calls = []

    def fake_get_tool_definitions(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("run_agent.get_tool_definitions", fake_get_tool_definitions)
    AIAgent(
        api_key="test-key",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **agent_kwargs,
    )
    assert calls
    return calls[0]


def test_xai_responses_skips_tool_search_bridge_assembly(monkeypatch):
    call = _capture_tool_registration(
        monkeypatch,
        provider="xai-oauth",
        base_url="https://api.x.ai/v1",
        api_mode="codex_responses",
    )

    assert call["skip_tool_search_assembly"] is True


def test_non_xai_responses_keeps_tool_search_bridge_assembly(monkeypatch):
    call = _capture_tool_registration(
        monkeypatch,
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="codex_responses",
    )

    assert call["skip_tool_search_assembly"] is False
