"""goals.auto_infer — a goal-less session whose reply commits to work gets an inferred goal.

Covers: judge parsing (true/false/garbage/short objective), fail-open on API error, the
``source="auto"`` tag surviving a JSON round trip + status line, never overwriting a user goal,
the config gate, and the gateway's user-authored-turn filter (heartbeat / continuation / internal
events must not seed goals)."""

from types import SimpleNamespace

import pytest

from hermes_cli import goals as goals_mod
from hermes_cli.goals import (
    GoalManager,
    GoalState,
    auto_infer_enabled,
    draft_contract,
    infer_goal_from_turn,
    maybe_infer_goal,
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Keep goal rows out of the real state DB and make the config gate deterministic."""
    store = {}

    def _save(session_id, state):
        store[session_id] = state.to_json()

    def _load(session_id):
        raw = store.get(session_id)
        return GoalState.from_json(raw) if raw else None

    monkeypatch.setattr(goals_mod, "save_goal", _save)
    monkeypatch.setattr(goals_mod, "load_goal", _load)
    monkeypatch.setattr(goals_mod, "clear_goal", lambda sid: store.pop(sid, None))
    monkeypatch.setattr(goals_mod, "auto_infer_enabled", lambda: True)
    # draft_contract makes its own aux call; stub to a plain contract.
    monkeypatch.setattr(goals_mod, "draft_contract", lambda objective, **kw: None)
    return store


def _judge(monkeypatch, reply):
    calls = []

    def fake(call_llm, system, prompt, timeout):
        calls.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(goals_mod, "_call_goal_judge_llm", fake)
    return calls


# ── infer_goal_from_turn ─────────────────────────────────────────────


def test_commitment_yields_objective(monkeypatch):
    calls = _judge(monkeypatch, '{"goal": true, "objective": "Cut release v1.2.13 once CI is green", "reason": "promised"}')
    out = infer_goal_from_turn("ship it", "I'll cut v1.2.13 once CI passes and post the link.")
    assert out == "Cut release v1.2.13 once CI is green"
    assert "ship it" in calls[0] and "cut v1.2.13" in calls[0]


def test_no_commitment_returns_none(monkeypatch):
    _judge(monkeypatch, '{"goal": false, "objective": "", "reason": "just an answer"}')
    assert infer_goal_from_turn("what is 2+2", "4") is None


def test_garbage_and_short_objective_return_none(monkeypatch):
    _judge(monkeypatch, "not json at all")
    assert infer_goal_from_turn("x", "I will do it") is None
    _judge(monkeypatch, '{"goal": true, "objective": "fix"}')
    assert infer_goal_from_turn("x", "I will fix") is None


def test_api_error_fails_open(monkeypatch):
    _judge(monkeypatch, RuntimeError("boom"))
    assert infer_goal_from_turn("x", "I'll handle the deploy") is None


def test_empty_reply_skips_judge(monkeypatch):
    calls = _judge(monkeypatch, '{"goal": true, "objective": "should not be reached"}')
    assert infer_goal_from_turn("x", "   ") is None
    assert calls == []


def test_multimodal_user_message_is_flattened(monkeypatch):
    calls = _judge(monkeypatch, '{"goal": false}')
    infer_goal_from_turn([{"type": "text", "text": "hello there"}, {"type": "image_url"}], "ok")
    assert "hello there" in calls[0]


# ── maybe_infer_goal / GoalManager.set_inferred ─────────────────────


def test_maybe_infer_sets_auto_goal_with_notice(monkeypatch):
    _judge(monkeypatch, '{"goal": true, "objective": "Verify crypto payment end to end and post the result"}')
    mgr = GoalManager(session_id="s1")
    notice = maybe_infer_goal(mgr, "check payments", "Next I'll run the live crypto test and report back.")
    assert notice and "Goal inferred" in notice and "/goal clear" in notice
    assert mgr.is_active()
    assert mgr.state.source == "auto"
    assert "inferred" in mgr.status_line()


def test_auto_goal_source_survives_round_trip():
    s = GoalState(goal="x", source="auto")
    assert GoalState.from_json(s.to_json()).source == "auto"
    # Older rows without the field load as user goals.
    assert GoalState.from_json('{"goal": "legacy"}').source == "user"


def test_user_goal_is_never_overwritten(monkeypatch):
    calls = _judge(monkeypatch, '{"goal": true, "objective": "Something totally different"}')
    mgr = GoalManager(session_id="s2")
    mgr.set("Ship the docs site")
    assert maybe_infer_goal(mgr, "u", "I'll do the other thing") is None
    assert mgr.state.goal == "Ship the docs site" and mgr.state.source == "user"
    assert calls == [], "judge must not even run when a goal exists"


def test_paused_goal_blocks_inference(monkeypatch):
    _judge(monkeypatch, '{"goal": true, "objective": "Something totally different"}')
    mgr = GoalManager(session_id="s3")
    mgr.set("Ship the docs site")
    mgr.pause("waiting on user")
    assert mgr.set_inferred("other") is None
    assert mgr.state.goal == "Ship the docs site"


def test_config_gate_off_skips_everything(monkeypatch):
    monkeypatch.setattr(goals_mod, "auto_infer_enabled", lambda: False)
    calls = _judge(monkeypatch, '{"goal": true, "objective": "Would have been a goal"}')
    mgr = GoalManager(session_id="s4")
    assert maybe_infer_goal(mgr, "u", "I'll ship it") is None
    assert calls == [] and not mgr.has_goal()


def test_auto_infer_config_defaults_and_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert auto_infer_enabled() is False
    (tmp_path / "config.yaml").write_text("goals:\n  auto_infer: true\n", encoding="utf-8")
    assert auto_infer_enabled() is True


def test_clear_drops_inferred_goal(monkeypatch):
    _judge(monkeypatch, '{"goal": true, "objective": "Cut release v1.2.13 once CI is green"}')
    mgr = GoalManager(session_id="s5")
    assert maybe_infer_goal(mgr, "u", "I'll cut the release") is not None
    mgr.clear()
    assert not mgr.has_goal()


# ── gateway: only real user turns may seed a goal ───────────────────


def test_turn_is_user_authored_filters_synthetic_events():
    from gateway.run_goals import GatewayGoalsMixin as M

    ok = SimpleNamespace(text="please ship it", internal=False)
    assert M._turn_is_user_authored(ok) is True
    assert M._turn_is_user_authored(None) is False
    assert M._turn_is_user_authored(SimpleNamespace(text="x", internal=True)) is False
    assert M._turn_is_user_authored(SimpleNamespace(text="x", internal=False, _heartbeat_session_id="h")) is False
    assert M._turn_is_user_authored(
        SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: x", internal=False)) is False
    assert M._turn_is_user_authored(
        SimpleNamespace(text="[Heartbeat — recurring instruction, fires every 30m]", internal=False)) is False


@pytest.mark.parametrize("user_rules", ["", "Do not change code. Stop if credentials are required."])
def test_inferred_contract_uses_exchange_and_only_user_rules(monkeypatch, user_rules):
    import json

    user = ("Once both hosts roll with K=0, verify live env on listen and pusher, then close the KB. " + user_rules).strip()
    reply = "I will verify listen at K=0, pusher at K=0, live env on both, then close the KB."
    checklist = "listen at K=0; pusher at K=0; live env verified on both; KB closed"
    captured = []

    def fake(call_llm, system, prompt, timeout):
        captured.append((system, prompt))
        if system == goals_mod.INFER_GOAL_SYSTEM_PROMPT:
            return json.dumps({"goal": True, "objective": "Verify both hosts and close the KB"})
        assert user in prompt and reply in prompt
        assert system != goals_mod.DRAFT_CONTRACT_SYSTEM_PROMPT
        assert "short checklist" in system
        assert "ONLY what the USER explicitly stated" in system
        assert "Never infer prohibitions" in system
        return json.dumps({
            "outcome": checklist, "verification": "Read K on each live host and check KB status",
            "constraints": "Do not change code" if user_rules else "",
            "boundaries": "", "stop_when": "credentials are required" if user_rules else "",
        })

    monkeypatch.setattr(goals_mod, "draft_contract", draft_contract)
    monkeypatch.setattr(goals_mod, "_call_goal_judge_llm", fake)
    manager = GoalManager("checklist")
    notice = maybe_infer_goal(manager, user, reply)
    contract = GoalManager("checklist").state.contract
    assert checklist in notice
    assert contract.outcome == checklist
    assert contract.constraints == ("Do not change code" if user_rules else "")
    assert contract.boundaries == ""
    assert contract.stop_when == ("credentials are required" if user_rules else "")
    assert len(captured) == 2
