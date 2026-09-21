"""Loop -> durable handoff -> fresh gateway session -> bounded chain, under A/B/A."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent.secret_scope import set_multiplex_active, get_secret
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.run_topics import GatewayTopicThreadsMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionStore, AsyncSessionStore, SessionSource
from hermes_constants import get_hermes_home
from hermes_state_continuation import read_checkpoint, claim_continuation
from tests.agent.test_checkpoint_continue import make_agent, install_model


class GatewayHarness(GatewayTurnMixin, GatewayTopicThreadsMixin):
    _TELEGRAM_GENERAL_TOPIC_IDS = GatewayRunner._TELEGRAM_GENERAL_TOPIC_IDS

    def _sync_session_db(self):
        return self.session_store._db

    def __init__(self, root):
        self.root = root
        self.session_store = SessionStore(root / "sessions", GatewayConfig())
        self.async_session_store = AsyncSessionStore(self.session_store)
        self.calls, self.notices = [], []

    def _profile_scope_for_source(self, source):
        return _profile_runtime_scope(self.root / "profiles" / source.profile)

    def _adapter_for_source(self, source):
        async def send(chat, text, metadata):
            self.notices.append((get_hermes_home(), chat, text, metadata, get_secret("TEST_SECRET")))
            return SimpleNamespace(success=True)
        return SimpleNamespace(send=send)

    def _thread_metadata_for_source(self, source):
        return {"thread_id": source.thread_id}

    async def _run_agent_inner(self, message, context_prompt, history, source, session_id, **kwargs):
        home = get_hermes_home()
        db = self.session_store._db_for_key(kwargs["session_key"])
        agent = make_agent(home, session_id, db)
        install_model(agent)
        self.calls.append((home, session_id, message, history, get_secret("TEST_SECRET")))
        try:
            result = await asyncio.to_thread(agent.run_conversation, message, conversation_history=history)
            return dict(result, session_id=agent.session_id)
        finally:
            agent._end_session_on_close = False
            agent.close()


def test_gateway_chain_and_profile_isolation(tmp_path, monkeypatch):
    asyncio.run(_gateway_chain_and_profile_isolation(tmp_path, monkeypatch))


async def _gateway_chain_and_profile_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    for name, maximum in (("A", 1), ("B", 0)):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            f"tools:\n  tool_search:\n    enabled: false\ncontinuation:\n  enabled: true\n  max_per_origin: {maximum}\n  soft_budget_fraction: 0.5\n", encoding="utf-8")
        (home / ".env").write_text(f"TEST_SECRET={name}-secret\n", encoding="utf-8")
    runner = GatewayHarness(root)
    set_multiplex_active(True)
    try:
        for index, name in enumerate(("A", "B", "A")):
            source = SessionSource(Platform.TELEGRAM, f"chat-{index}", thread_id="topic", profile=name, user_id="owner")
            with runner._profile_scope_for_source(source):
                entry = runner.session_store.get_or_create_session(source)
                key, sid = entry.session_key, entry.session_id
                runner._sync_session_db().enable_telegram_topic_mode(
                    chat_id=source.chat_id, user_id=source.user_id, profile_name=name)
            before = len(runner.calls)
            result = await runner._run_agent("Finish the report", "", [], source, sid, session_key=key)
            calls = runner.calls[before:]
            assert len(calls) == (2 if name == "A" else 1)
            assert all(call[0] == root / "profiles" / name and call[4] == f"{name}-secret" for call in calls)
            with runner._profile_scope_for_source(source):
                db = runner.session_store._db_for_key(key)
                assert Path(db.db_path) == root / "profiles" / name / "state.db"
                assert "Finish the report" in read_checkpoint(db, sid)["handoff"]
                if name == "A":
                    child = f"{sid}-cont-1"
                    assert calls[1][1] == child and calls[1][3] == []
                    assert "Next: step two" in calls[1][2]
                    assert db.get_messages_as_conversation(child)[0]["content"] == calls[1][2]
                    assert claim_continuation(db, sid, 1).status == "claimed"
                    assert claim_continuation(db, child, 1).status == "limit"
                    assert runner.session_store.lookup_by_session_key(key).session_id == child
                    binding = db.get_telegram_topic_binding(
                        chat_id=source.chat_id, thread_id=source.thread_id, profile_name=name)
                    assert binding["session_id"] == child
                assert result["continuation_ready"]
            assert runner.notices[-1][1] == source.chat_id
            assert "Owner action needed" in runner.notices[-1][2]
            assert runner.notices[-1][3]["thread_id"] == source.thread_id
            assert runner.notices[-1][4] == f"{name}-secret"
    finally:
        set_multiplex_active(False)
        runner.session_store.close_all_db_handles()
