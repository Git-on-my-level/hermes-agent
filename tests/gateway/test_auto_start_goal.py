import pytest
from unittest.mock import Mock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class _SessionEntry:
    session_id = "sid-auto-start-goal"


class _SessionStore:
    def get_or_create_session(self, source, **_kwargs):
        return _SessionEntry()


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _SessionStore()
    return runner


def _event(text: str, *, internal: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="chat", chat_type="channel", user_id="user"
        ),
        internal=internal,
    )


@pytest.mark.asyncio
async def test_auto_start_sets_goal_for_normal_external_message(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "goals:\n  auto_start: true\n  max_turns: 3\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()

    try:
        await GatewayRunner._auto_start_goal_for_inbound_event(_runner(), _event("finish the report"))
        state = goals.GoalManager("sid-auto-start-goal").state
        assert state is not None
        assert state.goal == "finish the report"
        assert state.max_turns == 3
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_auto_start_ignores_commands_and_internal_continuations(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  auto_start: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    manager = goals.GoalManager("sid-auto-start-goal")
    manager.set("keep this goal")

    try:
        runner = _runner()
        await GatewayRunner._auto_start_goal_for_inbound_event(runner, _event("/status"))
        await GatewayRunner._auto_start_goal_for_inbound_event(runner, _event("continue", internal=True))
        heartbeat = _event("[Heartbeat — recurring instruction]")
        heartbeat._heartbeat_session_id = "sid-auto-start-goal"
        await runner._auto_start_goal_for_inbound_event(heartbeat)
        await runner._auto_start_goal_for_inbound_event(
            _event("[Continuing toward your standing goal]\nGoal: keep this goal")
        )
        assert goals.GoalManager("sid-auto-start-goal").state.goal == "keep this goal"
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto_start", "inferred", "explicit", "paused"])
async def test_auto_start_and_inference_share_one_goal_loop(tmp_path, monkeypatch, mode):
    """Real config + SQLite + post-turn dispatch: one judge and at most one continuation."""
    (tmp_path / "config.yaml").write_text(
        f"goals:\n  auto_start: {'false' if mode == 'inferred' else 'true'}\n"
        "  auto_infer: true\n  max_turns: 3\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    infer = Mock(return_value="Finish the report and verify its contents")
    judge = Mock(return_value=("continue", "unfinished", False, None, False))
    monkeypatch.setattr(goals, "infer_goal_from_turn", infer)
    monkeypatch.setattr(goals, "draft_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(goals, "judge_goal", judge)
    runner = _runner()
    event = _event("finish the report")
    enqueued = []
    notices = []
    monkeypatch.setattr(runner, "_delivery_adapter_for", lambda source: object())
    monkeypatch.setattr(runner, "_session_key_for_source", lambda source: "key")
    monkeypatch.setattr(runner, "_enqueue_fifo", lambda key, event, adapter: enqueued.append(event))

    async def notice(source, text, **kwargs):
        notices.append(text)

    monkeypatch.setattr(runner, "_defer_goal_status_notice_after_delivery", notice)
    try:
        manager = goals.GoalManager("sid-auto-start-goal")
        if mode in ("explicit", "paused"):
            manager.set("Keep the explicit objective")
            if mode == "paused":
                manager.pause("waiting on user")
        await runner._auto_start_goal_for_inbound_event(event)
        await runner._run_post_turn_hooks(
            agent_result="I'll finish the report and verify its contents.",
            source=event.source, is_internal=False, event=event,
        )
        state = goals.GoalManager("sid-auto-start-goal").state
        assert state.source == {
            "auto_start": "auto_start", "inferred": "auto", "explicit": "user", "paused": "user",
        }[mode]
        assert infer.call_count == (1 if mode == "inferred" else 0)
        assert judge.call_count == (0 if mode == "paused" else 1)
        assert len(enqueued) == (0 if mode == "paused" else 1)
        inferred_notices = [text for text in notices if "Goal inferred" in text]
        assert len(inferred_notices) == (1 if mode == "inferred" else 0)
        expected = {
            "auto_start": "finish the report",
            "inferred": "Finish the report and verify its contents",
        }.get(mode, "Keep the explicit objective")
        assert state.goal == expected
        if enqueued:
            await runner._auto_start_goal_for_inbound_event(enqueued[0])
            assert goals.GoalManager("sid-auto-start-goal").state.goal == expected
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_auto_start_is_off_for_false_string(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  auto_start: 'false'\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()

    try:
        await GatewayRunner._auto_start_goal_for_inbound_event(_runner(), _event("do not start"))
        assert goals.GoalManager("sid-auto-start-goal").state is None
    finally:
        goals._DB_CACHE.clear()
