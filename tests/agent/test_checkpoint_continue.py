"""Real loop + SQLite: handoff boundaries preserve cached messages and hard budgets."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.continuation import ContinuationPolicy, WRAPUP_NOTICE
from hermes_state_continuation import read_checkpoint


def make_agent(home, session_id, db, *, platform="telegram", hard=5):
    from run_agent import AIAgent
    agent = AIAgent(
        session_id=session_id, session_db=db, model="test/model", provider="openai-compat",
        api_key="test", base_url="http://127.0.0.1:1/v1", max_iterations=hard,
        enabled_toolsets=["todo"], quiet_mode=True, skip_context_files=True,
        skip_memory=True, skip_background_review=True, platform=platform,
    )
    agent._cached_system_prompt = "Stable per-conversation prefix."
    agent.save_trajectories = False
    agent.compression_enabled = False
    return agent


def install_model(agent, *, ignore_wrapup=False):
    requests = []
    def model(kwargs):
        requests.append(deepcopy(kwargs))
        content = str(kwargs.get("messages"))
        if WRAPUP_NOTICE in content and not ignore_wrapup:
            msg = SimpleNamespace(content="Completed step one; output report.md. Next: step two. Uncommitted: report.md.", tool_calls=None)
        else:
            tc = SimpleNamespace(id=f"t{len(requests)}", type="function", function=SimpleNamespace(
                name="todo_list", arguments=json.dumps({"todos": [{"id": "1", "content": "wrote report.md", "status": "completed"}, {"id": "2", "content": f"verify report.md pass {len(requests)}", "status": "in_progress"}]})))
            msg = SimpleNamespace(content=None, tool_calls=[tc])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model", usage=None)
    agent._test_legacy_summary_calls = 0
    def legacy_summary(*_args):
        agent._test_legacy_summary_calls += 1
        return "Legacy iteration summary"
    agent._handle_max_iterations = legacy_summary
    agent._interruptible_api_call = model
    agent._interruptible_streaming_api_call = lambda kwargs, **_kw: model(kwargs)
    return requests


@pytest.mark.parametrize("ignore_wrapup", [False, True])
def test_loop_checkpoints_once_and_preserves_prefix(tmp_path, monkeypatch, ignore_wrapup):
    from hermes_state import SessionDB
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "tools:\n  tool_search:\n    enabled: false\ncontinuation:\n  enabled: true\n  soft_budget_fraction: 0.5\n", encoding="utf-8")
    db = SessionDB()
    agent = make_agent(tmp_path, "long-run", db)
    requests = install_model(agent, ignore_wrapup=ignore_wrapup)
    try:
        result = agent.run_conversation("Finish the report")
        assert result["continuation_ready"] and not result["completed"]
        checkpoint = read_checkpoint(db, agent.session_id)
        assert "Finish the report" in checkpoint["handoff"]
        assert "wrote report.md" in checkpoint["handoff"]
        rows = db.get_messages_as_conversation(agent.session_id)
        assert sum(WRAPUP_NOTICE in str(row.get("content")) for row in rows) == 1
        assert rows[-1]["role"] == "assistant" and "Resume" in rows[-1]["content"]
        assert sum(row["role"] == "user" for row in rows) == 1
        assert agent._test_legacy_summary_calls == 0
        assert len(requests) <= agent.max_iterations
        for earlier, later in zip(requests, requests[1:]):
            before, after = earlier["messages"], later["messages"]
            assert after[:len(before)] == before
            assert later.get("tools") == earlier.get("tools")
        if not ignore_wrapup:
            assert "Next: step two" in checkpoint["handoff"]
    finally:
        agent._end_session_on_close = False
        agent.close()
        db.close()


@pytest.mark.parametrize("raw", [
    {"enabled": "false"}, {"max_per_origin": -1}, {"max_per_origin": True},
    {"soft_budget_fraction": float("nan")}, {"soft_budget_fraction": 1},
])
def test_invalid_policy_is_rejected(raw):
    with pytest.raises(ValueError):
        ContinuationPolicy.from_config({"continuation": raw})


def test_manual_resume_of_unstarted_child_uses_durable_seed_once(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_state_continuation import save_checkpoint, claim_continuation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("origin", "telegram")
    handoff = json.dumps({"objective": "Finish the report", "state_and_next_steps": "report.md is written; verify totals"})
    save_checkpoint(db, "origin", handoff, "iteration_cap")
    child = claim_continuation(db, "origin", 1)
    agent = make_agent(tmp_path, child.session_id, db)
    requests = []
    def respond(kwargs, **_kw):
        requests.append(deepcopy(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Totals verified.", tool_calls=None), finish_reason="stop")], usage=None)
    agent._interruptible_api_call = respond
    agent._interruptible_streaming_api_call = respond
    try:
        first = agent.run_conversation("Continue")
        users = [m for m in db.get_messages_as_conversation(child.session_id) if m["role"] == "user"]
        assert len(users) == 1 and handoff in users[0]["content"]
        assert users[0]["content"].endswith("Continue")
        second = agent.run_conversation("Explain the totals", conversation_history=first["messages"])
        users = [m for m in second["messages"] if m["role"] == "user"]
        assert users[-1]["content"] == "Explain the totals"
        assert sum(child.seed in str(m.get("content")) for m in users) == 1
    finally:
        agent._end_session_on_close = False
        agent.close()
        db.close()
