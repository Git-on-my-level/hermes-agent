"""Default-off, ordered route decisions at explicit cache boundaries.

The local ledger is an opt-in token proxy, not credits; it cannot see other
hosts sharing a key. Because Hermes has no plan token allowance, that signal
is unavailable unless the quota supplies a positive ``token_limit``. Ledger
rows are session/model aggregates attributed by last_seen, not an exact meter.
Signal readers are injected into the decision function; no I/O occurs there.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_WINDOWS = {"5h": {"session", "current session", "5h"},
            "weekly": {"weekly", "current week", "weekly (all models)"},
            "monthly": {"monthly", "current month"}}


def _schedule_matches(schedule, now):
    local = now.astimezone(ZoneInfo(schedule["tz"]))
    start = datetime.strptime(schedule["start"], "%H:%M").time()
    end = datetime.strptime(schedule["end"], "%H:%M").time()
    clock = local.time().replace(tzinfo=None)
    # Overnight windows belong to the day on which they start.
    day = local - timedelta(days=1) if start > end and clock < end else local
    return (("mon", "tue", "wed", "thu", "fri", "sat", "sun")[day.weekday()] in schedule.get("days", [])
            and (start <= clock < end if start <= end else clock >= start or clock < end))


def _signal_matches(condition, now, account_usage, local_ledger):
    if not isinstance(condition, dict) or len(condition) != 1:
        return False
    if "any" in condition or "all" in condition:
        kind = next(iter(condition))
        children = condition[kind]
        if not isinstance(children, list) or not children:
            return False
        return (any if kind == "any" else all)(
            _signal_matches(child, now, account_usage, local_ledger) for child in children)
    if "schedule" in condition:
        return _schedule_matches(condition["schedule"], now)
    quota = condition.get("quota")
    if not isinstance(quota, dict) or quota.get("window") not in _WINDOWS:
        return False
    threshold = float(quota["remaining_below"])
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        return False
    if quota.get("source") == "local_ledger":
        remaining = local_ledger(quota, now) if local_ledger else None
        return remaining is not None and math.isfinite(remaining) and remaining < threshold
    if quota.get("source") != "account_usage" or not account_usage:
        return False
    snapshot = account_usage(quota["provider"])
    if snapshot is None or not snapshot.available:
        return False
    labels = _WINDOWS[quota["window"]]
    return any(window.label.lower() in labels and window.used_percent is not None
               and math.isfinite(window.used_percent) and 1 - window.used_percent / 100 < threshold
               for window in snapshot.windows)


def evaluate_route_policy(config, *, scope, boundary, now, current_override=None,
                          idle_seconds=None, account_usage=None, local_ledger=None):
    """Return a persistable override or None (no match), with no side effects.

    Explicit overrides always win. A policy override remains sticky on warm
    turns; only a matching explicitly authorized turn/cache-cold rule can replace it.
    Invalid or unavailable signals never match.
    """
    if current_override and not current_override.get("route_policy"):
        return current_override
    policy = (config or {}).get("route_policy") or {}
    if policy.get("enabled") is not True:
        return current_override if boundary == "turn" else None
    keep_current = boundary == "turn"
    for rule in policy.get("rules", []):
        try:
            scopes = rule.get("scope") or {}
            if scope not in scopes.get("include", ["gateway_chat"]) or scope in scopes.get("exclude", []):
                continue
            events = rule.get("apply_at", ["new_session", "post_compaction"])
            allowed = boundary in events and boundary in {"new_session", "post_compaction"}
            if boundary == "turn":
                allowed = "turn" in events and rule.get("accept_cache_miss") is True
                ttl = rule.get("cache_cold_after")
                allowed = allowed or ("cache_cold" in events and ttl is not None and float(ttl) > 0
                                      and idle_seconds is not None and idle_seconds >= float(ttl))
            if not allowed:
                continue
            if current_override and rule.get("name") == current_override.get("route_policy_rule"):
                keep_current = False
            if not _signal_matches(rule["when"], now, account_usage, local_ledger):
                continue
            route = rule["route"]
            if not route.get("provider") or not route.get("model"):
                continue
            return {**{key: route[key] for key in ("provider", "model", "base_url") if route.get(key)},
                    "route_policy": True, "route_policy_rule": str(rule["name"])}
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return current_override if keep_current else None


def _rules_need_idle(config) -> bool:
    for rule in ((config or {}).get("route_policy") or {}).get("rules") or []:
        if not isinstance(rule, dict):
            continue
        events = rule.get("apply_at") or ["new_session", "post_compaction"]
        if "cache_cold" in events and rule.get("cache_cold_after") not in (None, "", 0):
            return True
    return False


def _idle_from_stamp(stamp, now):
    if isinstance(stamp, (int, float)):
        return now.timestamp() - float(stamp)
    if isinstance(stamp, str):
        try:
            return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
        except (ValueError, TypeError):
            return None
    return None


def _prefix_facts(store, session_id, now, *, need_idle: bool):
    """(has_turn, idle_seconds) without message bodies.

    ponytail: one indexed probe. A store with no db falls back to load_transcript.
    """
    db = getattr(store, "_db", None)
    if db is None or not session_id:
        history = store.load_transcript(session_id) if session_id else []
        has_turn = any(
            isinstance(row, dict) and row.get("role") not in {None, "session_meta", "system"}
            for row in history)
        if not need_idle:
            return has_turn, None
        from gateway.run import _last_transcript_timestamp
        return has_turn, _idle_from_stamp(_last_transcript_timestamp(history), now)
    if need_idle:
        row = db._read_one(
            "SELECT timestamp FROM messages WHERE session_id = ? "
            "AND role NOT IN ('session_meta', 'system') ORDER BY id DESC LIMIT 1",
            (session_id,))
        if not row:
            return False, None
        return True, _idle_from_stamp(row["timestamp"], now)
    row = db._read_one(
        "SELECT 1 FROM messages WHERE session_id = ? "
        "AND role NOT IN ('session_meta', 'system') LIMIT 1",
        (session_id,))
    return row is not None, None


def _ledger_remaining(store, quota, now):
    limit = float(quota.get("token_limit", 0))
    db = getattr(store, "_db", None)
    if limit <= 0 or not math.isfinite(limit) or db is None:
        return None
    seconds = {"5h": 18000, "weekly": 604800, "monthly": 30 * 86400}[quota["window"]]
    row = db._read_one(
        "SELECT SUM(input_tokens + output_tokens) AS tokens FROM session_model_usage "
        "WHERE billing_provider = ? AND last_seen >= ?",
        (quota["provider"], now.timestamp() - seconds))
    return 1 - float(row["tokens"] or 0) / limit


def apply_gateway_policy(runner, key, source, config, *, boundary=None, now=None, agent=None):
    """Thin synchronous applier using /model's store and credential resolver.

    Called under the gateway's profile scope (including its compaction worker).
    Store commits precede publication; a failed client switch leaves the prior
    override retryable. Explicit /model state is never modified.
    """
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    state = runner._peek_session_state(key)
    current = state.conversation.model_override if state else None
    if current and not current.get("route_policy"):
        return current
    try:
        if config is None:
            from gateway.run import _load_gateway_config
            config = _load_gateway_config()
        if ((config or {}).get("route_policy") or {}).get("enabled") is not True and not current:
            return current
        store = getattr(runner, "session_store", None)
        if store is None or source is None:
            return current
        entry = store.lookup_by_session_key(key)
        if entry is None:
            return current
        # This entry point is gateway chat only. Cron/delegation/auxiliary never
        # enter it; pinned channel routes take the same precedence as explicit /model.
        if runner._channel_override_for(source):
            return current
        now = now or datetime.now(timezone.utc)
        # updated_at is touched by intake, so it cannot measure prefix inactivity.
        # Do not load_transcript here: that replays every message body on each
        # gateway turn once the policy is enabled.
        need_idle = _rules_need_idle(config)
        if boundary is None or need_idle:
            has_turn, idle = _prefix_facts(store, entry.session_id, now, need_idle=need_idle)
            if boundary is None:
                boundary = "turn" if has_turn else "new_session"
        else:
            idle = None

        def account_usage(provider):
            from agent.account_usage import fetch_account_usage
            try:
                runtime = _resolve_runtime_agent_kwargs_for_provider(provider)
                return fetch_account_usage(provider, api_key=runtime.get("api_key"), base_url=runtime.get("base_url"))
            except Exception:
                return None

        target = evaluate_route_policy(
            config, scope="gateway_chat", boundary=boundary, now=now, current_override=current,
            idle_seconds=idle, account_usage=account_usage,
            local_ledger=lambda quota, stamp: _ledger_remaining(store, quota, stamp))
        if target == current:
            return current
        runtime = None
        if target:
            runtime = _resolve_runtime_agent_kwargs_for_provider(target["provider"], target_model=target["model"])
        if agent is not None:
            if target:
                model = target["model"]
            else:
                # Resolve primary without the policy override; channel pins were
                # excluded above. The regular resolver also handles provider defaults.
                from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model
                runtime = _resolve_runtime_agent_kwargs()
                model = runtime.pop("model", None) or _resolve_gateway_model(config)
        if runner._session_model_override(key) != current:
            return runner._session_model_override(key)  # a concurrent explicit /model wins
        store.set_model_override(key, target)
        if agent is not None:
            try:
                agent.switch_model(model, runtime["provider"], api_key=runtime.get("api_key") or "",
                                   base_url=(target or {}).get("base_url") or runtime.get("base_url") or "",
                                   api_mode=runtime.get("api_mode") or "", capabilities=runtime.get("capabilities"))
            except Exception:
                # switch_model rolls back its bound client on failure.
                store.set_model_override(key, current)
                raise
        override = {**(runtime or {}), **target} if target else None
        runner._session_state(key).conversation.model_override = override
        if target and not store.get_session_metadata(key, "route_policy_notified", False):
            notice = (f"Route policy {target['route_policy_rule']}: using {target['model']} via "
                      f"{target['provider']} until the next compaction or new session.")
            # Detached/manual compressors may have no delivery callback. Keep
            # their notice for the actual turn instead of losing it in a probe.
            if agent is not None and callable(getattr(agent, "status_callback", None)):
                agent._emit_status(notice)
            else:
                runner._session_state(key).conversation.route_policy_notice = notice
            store.set_session_metadata(key, "route_policy_notified", True)
        return override
    except Exception:
        logger.warning("Route policy unavailable; keeping the current route", exc_info=True)
        return current


def apply_compaction_policy(agent, old_session_id=None):
    """Find the owning gateway lane; CLI, cron and delegated agents are out of scope."""
    from gateway.run import _gateway_runner_ref
    runner = _gateway_runner_ref()
    if runner is None:
        return
    if getattr(agent, "platform", None) in {"cron", "subagent", "auxiliary"}:
        return
    store = runner.session_store
    key = getattr(agent, "_gateway_session_key", None)
    entry = store.lookup_by_session_key(key) if key else store.lookup_by_session_id(old_session_id or agent.session_id)
    if entry is None:
        return
    state = runner._peek_session_state(entry.session_key)
    # Detached hygiene agents carry the gateway's explicit sentinel instead of
    # a chat key. They compact the owning lane before it rebinds the routing row.
    from gateway.run import _cached_agent_for_hygiene, _GATEWAY_HYGIENE_PLATFORM
    if not (key or getattr(agent, "platform", None) == _GATEWAY_HYGIENE_PLATFORM
            or (state and state.turn.agent is agent)
            or _cached_agent_for_hygiene(runner, entry.session_key) is agent):
        return
    from hermes_cli.config import load_config
    apply_gateway_policy(runner, entry.session_key, entry.origin, load_config(),
                         boundary="post_compaction", agent=agent)
