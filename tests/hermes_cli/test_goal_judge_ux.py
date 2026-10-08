"""Checklist completion gaps get one repair turn before asking the user."""

import json

import pytest

from hermes_cli import goals


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager("checklist-session")
    mgr.set("Roll both hosts to K=0 and close the KB", contract=goals.GoalContract(
        outcome="listen at K=0; pusher at K=0; live env verified on both; KB closed",
        verification="Read the live env on each host and inspect the KB status",
    ))
    yield mgr
    goals._DB_CACHE.clear()


def test_gap_repairs_once_then_pauses_and_persists(manager, monkeypatch):
    prompts = []

    def judge(call_llm, system, prompt, timeout):
        prompts.append((system, prompt))
        return json.dumps({"verdict": "gap", "reason": "pusher is still K=20 and its live env is unverified"})

    monkeypatch.setattr(goals, "_call_goal_judge_llm", judge)
    first = manager.evaluate_after_turn("Done: listen is K=0 and the KB is closed. Pusher is K=20.")
    assert first["verdict"] == "gap"
    assert first["status"] == "active" and first["should_continue"]
    assert first["notice_level"] == "debug"
    prompt = first["continuation_prompt"]
    assert first["reason"] in prompt
    assert "Finish them now" in prompt
    assert "ONE decision request" in prompt
    assert all(item in prompt for item in ("what is missing", "why", "options", "your default"))
    assert "GAP" in prompts[0][0] and "GAP" in prompts[0][1]
    assert manager.state.paused_reason is None
    manager = goals.GoalManager(manager.session_id)
    assert manager.state.consecutive_gaps == 1
    second = manager.evaluate_after_turn("Everything is done; pusher remains K=20.")
    assert second["verdict"] == "gap"
    assert second["status"] == "paused" and not second["should_continue"]
    assert second["continuation_prompt"] is None
    assert second["notice_level"] == "important"
    assert "needs your input" in second["message"]
    assert second["reason"] in second["message"]
    assert goals.GoalManager(manager.session_id).state.consecutive_gaps == 2
    assert goals.GoalState.from_json('{"goal":"legacy"}').consecutive_gaps == 0
    assert "gap" in manager.status_line()
    manager.resume()
    assert manager.state.consecutive_gaps == 0


@pytest.mark.parametrize("verdict", ["continue", "wait", "done", "blocked"])
def test_non_gap_verdict_resets_gap_streak(manager, monkeypatch, verdict):
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("gap", "pusher missing", False, None, False))
    manager.evaluate_after_turn("Done")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: (
        verdict, "need user authorization" if verdict == "blocked" else "progress observed",
        False, {"seconds": 60} if verdict == "wait" else None, False,
    ))
    decision = manager.evaluate_after_turn("Progress")
    assert goals.GoalManager(manager.session_id).state.consecutive_gaps == 0
    assert decision["notice_level"] == {"done": "info", "blocked": "important"}.get(verdict, "debug")
    assert "unachievable" not in decision["message"]
    if verdict == "blocked":
        assert "needs your input" in decision["message"]
    elif verdict == "continue":
        assert "progress observed" in manager.status_line()
        assert "\n" not in manager.status_line()
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("gap", "pusher missing", False, None, False))
        assert manager.evaluate_after_turn("Done again")["should_continue"]


@pytest.mark.parametrize("kind", ["budget", "parse", "transport", "gate_pause", "gate_retry", "waiting"])
def test_goal_notice_levels_cover_failure_and_wait_paths(manager, monkeypatch, kind):
    if kind == "budget":
        manager.state.max_turns = 1
    if kind == "parse":
        manager.state.consecutive_parse_failures = goals.DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES - 1
    if kind == "transport":
        manager.state.consecutive_transport_failures = goals.DEFAULT_MAX_CONSECUTIVE_TRANSPORT_FAILURES - 1
    if kind.startswith("gate"):
        gate = manager.add_gate("verify", max_retries=1)
        gate.attempts = 1 if kind == "gate_pause" else 0
        monkeypatch.setattr(goals, "run_gate", lambda *a, **kw: (False, 1, "verification failed"))
    if kind == "waiting":
        manager.wait_for_seconds(60, "CI running")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: (
        "continue", "unfinished", kind == "parse", None, kind == "transport",
    ))
    decision = manager.evaluate_after_turn("Still working")
    assert decision["notice_level"] == ("debug" if kind in ("gate_retry", "waiting") else "important")
    assert decision["message"]
    if kind == "gate_retry":
        assert "gate_failed" in manager.status_line()
