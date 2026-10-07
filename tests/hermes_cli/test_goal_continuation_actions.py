"""Fork adaptations of upstream #129380 and #134448: enabled goal-loop contracts."""

from unittest.mock import Mock

import pytest

from hermes_cli import goals


@pytest.fixture
def goal_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield tmp_path
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("criteria", ["plain", "contract", "subgoals"])
def test_goal_continuation_requires_action_with_all_criteria(goal_home, criteria):
    manager = goals.GoalManager("action-goal")
    contract = goals.GoalContract(outcome="Release exists", verification="CI passes")
    if criteria == "contract":
        manager.set_inferred("Ship the release after CI passes", contract=contract)
    else:
        manager.set("Ship the release after CI passes")
    if criteria == "subgoals":
        manager.add_subgoal("Verify the signed artifact")
    prompt = manager.next_continuation_prompt()
    assert manager.state.goal in prompt
    assert "Do not repeat a status update" in prompt
    assert "tool to take one concrete step before replying" in prompt
    if criteria == "contract":
        assert contract.verification in prompt
    if criteria == "subgoals":
        assert manager.state.subgoals[0] in prompt


def test_auto_infer_false_string_does_not_call_judge(goal_home, monkeypatch):
    (goal_home / "config.yaml").write_text("goals:\n  auto_infer: 'false'\n", encoding="utf-8")
    judge = Mock(return_value='{"goal": true, "objective": "Ship the release after CI passes"}')
    monkeypatch.setattr(goals, "_call_goal_judge_llm", judge)
    manager = goals.GoalManager("disabled-inference")
    assert goals.maybe_infer_goal(manager, "ship it", "I'll ship it after CI passes") is None
    assert not manager.has_goal()
    judge.assert_not_called()
