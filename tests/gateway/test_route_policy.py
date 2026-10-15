"""Exercise policy persistence and resolution against a real, isolated session store."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.route_policy import apply_gateway_policy
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore

NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
CONFIG = {"model": {"default": "primary"}, "route_policy": {"enabled": True, "rules": [{
    "name": "peak", "when": {"schedule": {"tz": "Asia/Singapore", "days": ["mon"], "start": "12:00", "end": "18:00"}},
    "scope": {"include": ["gateway_chat"], "exclude": ["pinned", "cron", "delegation", "auxiliary"]},
    "apply_at": ["new_session", "post_compaction"], "sticky": "boundary",
    "route": {"provider": "xai-oauth", "model": "grok-4.7", "base_url": "https://api.x.ai/v1"},
}]}}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.run as run
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u", chat_id="c", chat_type="dm")
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = store
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner.config = GatewayConfig()
    # Credential transport is the fake; store, rehydration and resolver are real.
    monkeypatch.setattr(run, "_resolve_runtime_agent_kwargs_for_provider", lambda provider, **kw: {
        "provider": provider, "api_key": "destination-secret", "api_mode": "chat_completions",
        "base_url": "https://api.x.ai/v1", "capabilities": {},
    })
    monkeypatch.setattr(run, "_resolve_runtime_agent_kwargs", lambda: {
        "provider": "primary-provider", "api_key": "primary-secret", "base_url": "https://primary.example/v1"})
    monkeypatch.setattr(run, "_load_gateway_config", lambda: CONFIG)
    yield runner, store, source, entry
    if store._db is not None:
        store._db.close()


def test_resolver_does_not_clobber_explicit_override(lane):
    runner, store, source, entry = lane
    explicit = {"model": "explicit", "provider": "anthropic"}
    store.set_model_override(entry.session_key, explicit)
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=CONFIG)
    assert model == "explicit"
    assert runtime["provider"] == "anthropic"
    assert store.get_model_override(entry.session_key) == explicit
    assert not store.get_session_metadata(entry.session_key, "route_policy_notified")


def test_policy_persists_rehydrates_and_keeps_warm_route(lane):
    runner, store, source, entry = lane
    key = entry.session_key
    apply_gateway_policy(runner, key, source, CONFIG, now=NOW)
    assert store.get_model_override(key)["route_policy"] is True
    assert "destination-secret" not in str(store.get_model_override(key))
    assert "Route policy peak:" in runner._session_state(key).conversation.route_policy_notice
    runner._session_model_overrides = {}
    runner._rehydrate_session_model_override(key)
    assert runner._session_model_override(key)["route_policy"] is True
    # A transcript makes this a warm session; leaving the schedule must not switch.
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "hello"})
    apply_gateway_policy(runner, key, source, CONFIG, now=NOW.replace(hour=11))
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=CONFIG)
    assert (model, runtime["provider"], runtime["base_url"]) == ("grok-4.7", "xai-oauth", "https://api.x.ai/v1")
    assert store.get_session_metadata(key, "route_policy_notified") is True


@pytest.mark.parametrize("matching", [True, False])
def test_successful_compaction_re_evaluates_policy(lane, monkeypatch, matching):
    import gateway.run as run
    from agent import route_policy
    from agent.conversation_compression import _finish_compaction_boundary
    runner, store, source, entry = lane
    key = entry.session_key
    apply_gateway_policy(runner, key, source, CONFIG, now=NOW)
    agent = SimpleNamespace(
        _gateway_session_key=key, session_id=entry.session_id, platform="telegram", tools=[],
        context_compressor=SimpleNamespace(compression_count=0), _memory_manager=None,
        switch_model=Mock(), _emit_status=Mock(), event_callback=None,
    )
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: CONFIG)
    real_apply = route_policy.apply_gateway_policy
    monkeypatch.setattr(route_policy, "apply_gateway_policy", lambda *args, **kw: real_apply(
        *args, **kw, now=NOW if matching else NOW.replace(hour=11)))
    _finish_compaction_boundary(
        agent, [{"role": "user", "content": "summary"}], new_system_prompt="", old_session_id=None,
        in_place=True, compacted_in_place=True, session_commit_succeeded=True,
        defer_context_engine_notification=True, compression_made_progress=True,
        compression_used_fallback=False, compression_feasibility_skip=False, task_id="test",
    )
    if matching:
        assert store.get_model_override(key)["model"] == "grok-4.7"
        assert agent.switch_model.call_args.args[:2] == ("grok-4.7", "xai-oauth")
    else:
        assert store.get_model_override(key) is None
        assert runner._session_model_override(key) is None
        assert agent.switch_model.call_args.args[:2] == ("primary", "primary-provider")


def test_switch_failure_preserves_override(lane):
    runner, store, source, entry = lane
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW)
    prior = store.get_model_override(entry.session_key)
    agent = SimpleNamespace(switch_model=Mock(side_effect=RuntimeError("client failed")))
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW.replace(hour=11),
                         boundary="post_compaction", agent=agent)
    assert store.get_model_override(entry.session_key) == prior


@pytest.mark.parametrize("committed,progress", [(False, True), (True, False)])
def test_unsuccessful_compaction_does_not_invoke_policy(lane, monkeypatch, committed, progress):
    from agent.conversation_compression import _finish_compaction_boundary
    from agent import route_policy
    runner, store, source, entry = lane
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW)
    prior = store.get_model_override(entry.session_key)
    callback = Mock(side_effect=AssertionError("not a committed boundary"))
    monkeypatch.setattr(route_policy, "apply_compaction_policy", callback)
    agent = SimpleNamespace(session_id=entry.session_id, platform="telegram", tools=[], _memory_manager=None,
                            context_compressor=SimpleNamespace(compression_count=0), event_callback=None)
    _finish_compaction_boundary(
        agent, [], new_system_prompt="", old_session_id=None, in_place=True, compacted_in_place=True,
        session_commit_succeeded=committed, defer_context_engine_notification=True,
        compression_made_progress=progress, compression_used_fallback=False,
        compression_feasibility_skip=False, task_id="test")
    callback.assert_not_called()
    assert store.get_model_override(entry.session_key) == prior


def test_hygiene_compaction_uses_owning_gateway_lane(lane, monkeypatch):
    import gateway.run as run
    from agent.route_policy import apply_compaction_policy
    runner, store, source, entry = lane
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW)
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"route_policy": {"enabled": True, "rules": []}})
    agent = SimpleNamespace(session_id=entry.session_id, platform=run._GATEWAY_HYGIENE_PLATFORM, switch_model=Mock())
    apply_compaction_policy(agent)
    assert store.get_model_override(entry.session_key) is None
    agent.switch_model.assert_called_once()


@pytest.mark.parametrize("scope", ["cron", "subagent", "auxiliary"])
def test_non_chat_agents_do_not_change_route(lane, monkeypatch, scope):
    import gateway.run as run
    from agent.route_policy import apply_compaction_policy
    runner, store, source, entry = lane
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW)
    prior = store.get_model_override(entry.session_key)
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    agent = SimpleNamespace(session_id=entry.session_id, _gateway_session_key=entry.session_key,
                            platform=scope, switch_model=Mock())
    apply_compaction_policy(agent)
    assert store.get_model_override(entry.session_key) == prior
    agent.switch_model.assert_not_called()


def test_new_session_resolver_diverts_with_destination_credentials(lane, monkeypatch):
    import agent.route_policy as policy
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(policy, "datetime", Clock)
    runner, store, source, entry = lane
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=CONFIG)
    assert model == "grok-4.7"
    assert (runtime["provider"], runtime["api_key"], runtime["base_url"]) == (
        "xai-oauth", "destination-secret", "https://api.x.ai/v1")
    assert store.get_model_override(entry.session_key)["route_policy_rule"] == "peak"
    assert runner._session_state(entry.session_key).conversation.route_policy_notice == (
        "Route policy peak: using grok-4.7 via xai-oauth until the next compaction or new session.")


def test_enabled_policy_does_not_replay_transcript(lane, monkeypatch):
    runner, store, source, entry = lane
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "hello"})

    def boom(*_a, **_k):
        raise AssertionError("route policy replayed the transcript")

    monkeypatch.setattr(store, "load_transcript", boom)
    model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=CONFIG)
    # A session that already has a turn is not new, so the peak window must not divert it.
    assert (model, runtime["provider"]) == ("primary", "primary-provider")
    assert apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW) is None
    assert store.get_model_override(entry.session_key) is None


def test_pinned_channel_is_not_diverted(lane, monkeypatch):
    runner, store, source, entry = lane
    monkeypatch.setattr(runner, "_channel_override_for", lambda source: SimpleNamespace(model="pin"))
    assert apply_gateway_policy(runner, entry.session_key, source, CONFIG, now=NOW) is None
    assert store.get_model_override(entry.session_key) is None


def test_opt_in_local_ledger_uses_token_allowance(lane):
    from agent.route_policy import _ledger_remaining
    runner, store, source, entry = lane
    db = store._db
    db.update_token_counts(entry.session_id, model="primary", billing_provider="example",
                           input_tokens=70, output_tokens=15, api_call_count=1)
    quota = {"provider": "example", "window": "5h"}
    assert _ledger_remaining(store, quota, datetime.now(timezone.utc)) is None
    quota["token_limit"] = 100
    assert _ledger_remaining(store, quota, datetime.now(timezone.utc)) == pytest.approx(.15)


def test_post_compaction_preserves_explicit_model(lane):
    runner, store, source, entry = lane
    explicit = {"provider": "anthropic", "model": "user-choice", "api_key": "user-secret"}
    runner._session_model_overrides[entry.session_key] = explicit
    store.set_model_override(entry.session_key, explicit)
    agent = SimpleNamespace(switch_model=Mock())
    result = apply_gateway_policy(runner, entry.session_key, source, CONFIG, boundary="post_compaction", now=NOW, agent=agent)
    assert result is explicit
    agent.switch_model.assert_not_called()
    assert store.get_model_override(entry.session_key)["model"] == "user-choice"


def test_detached_compaction_queues_first_diversion_notice(lane):
    runner, store, source, entry = lane
    agent = SimpleNamespace(switch_model=Mock())  # detached compressor has no delivery callback
    apply_gateway_policy(runner, entry.session_key, source, CONFIG, boundary="post_compaction", now=NOW, agent=agent)
    assert "Route policy peak:" in runner._session_state(entry.session_key).conversation.route_policy_notice
    agent.switch_model.assert_called_once()
