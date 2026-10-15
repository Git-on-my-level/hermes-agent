from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from agent.route_policy import evaluate_route_policy

RULE = {
    "name": "peak", "when": {"schedule": {"tz": "Asia/Singapore", "days": ["mon", "tue", "wed", "thu", "fri"],
                                          "start": "12:00", "end": "18:00"}},
    "scope": {"include": ["gateway_chat"], "exclude": ["cron", "delegation", "auxiliary", "pinned"]},
    "apply_at": ["new_session", "post_compaction"], "sticky": "boundary",
    "route": {"provider": "xai-oauth", "model": "grok-4.7", "base_url": "https://api.x.ai/v1"},
}
NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)  # Monday noon in Singapore


def decide(rule=None, **kwargs):
    return evaluate_route_policy({"route_policy": {"enabled": True, "rules": [rule or RULE]}},
                                 **{"scope": "gateway_chat", "boundary": "new_session", "now": NOW, **kwargs})


@pytest.mark.parametrize("config", [{}, {"route_policy": {"enabled": False, "rules": [RULE]}}])
def test_default_off(config):
    assert evaluate_route_policy(config, scope="gateway_chat", boundary="new_session", now=NOW) is None


@pytest.mark.parametrize("day,hour,minute,match", [(12, 3, 59, False), (12, 4, 0, True), (12, 9, 59, True),
                                                (12, 10, 0, False), (17, 4, 0, False)])
def test_singapore_window(day, hour, minute, match):
    assert bool(decide(now=datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc))) is match


@pytest.mark.parametrize("scope", ["cron", "delegation", "auxiliary", "pinned"])
def test_scope_exclusions(scope):
    assert decide(scope=scope) is None


def test_explicit_override_and_sticky_warm_prefix():
    explicit = {"provider": "anthropic", "model": "explicit"}
    assert decide(current_override=explicit) is explicit
    stored = decide()
    later = NOW.replace(hour=11)
    assert decide(boundary="turn", now=later, current_override=stored) is stored
    assert decide(boundary="post_compaction", now=later, current_override=stored) is None
    assert decide(boundary="post_compaction", current_override=stored)["model"] == "grok-4.7"


def test_turn_requires_cache_miss_consent():
    rule = deepcopy(RULE)
    rule["apply_at"] = ["turn"]
    assert decide(rule, boundary="turn") is None
    rule["accept_cache_miss"] = True
    assert decide(rule, boundary="turn")["model"] == "grok-4.7"


def test_cache_cold_has_no_default_ttl():
    rule = deepcopy(RULE)
    rule["apply_at"] = ["cache_cold"]
    assert decide(rule, boundary="turn", idle_seconds=99999) is None
    rule["cache_cold_after"] = 600
    assert decide(rule, boundary="turn", idle_seconds=599) is None
    assert decide(rule, boundary="turn", idle_seconds=None) is None
    assert decide(rule, boundary="turn", idle_seconds=600)


@pytest.mark.parametrize("window,label", [("5h", "Session"), ("5h", "Current session"),
                                         ("weekly", "Weekly"), ("weekly", "Current week"), ("monthly", "Monthly")])
def test_account_quota_and_unavailable(window, label):
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    rule = deepcopy(RULE)
    rule["when"] = {"quota": {"provider": "openai-codex", "window": window,
                               "remaining_below": .25, "source": "account_usage"}}
    snapshot = AccountUsageSnapshot("openai-codex", "fake", NOW, windows=(AccountUsageWindow(label, 80),))
    ledger = Mock(side_effect=AssertionError("ledger was not opted in"))
    assert decide(rule, account_usage=lambda p: snapshot, local_ledger=ledger)
    assert decide(rule, account_usage=lambda p: None, local_ledger=ledger) is None
    ledger.assert_not_called()
    snapshot = AccountUsageSnapshot("openai-codex", "fake", NOW, windows=(AccountUsageWindow(label, 75),))
    assert decide(rule, account_usage=lambda p: snapshot) is None


def test_opt_in_ledger_and_composite_signals():
    quota = {"quota": {"provider": "example", "window": "weekly", "remaining_below": .25, "source": "local_ledger"}}
    rule = deepcopy(RULE)
    rule["when"] = {"all": [RULE["when"], quota]}
    account = Mock(side_effect=AssertionError("account API not selected"))
    assert decide(rule, local_ledger=lambda q, n: .1, account_usage=account)
    assert decide(rule, local_ledger=lambda q, n: None) is None
    rule["when"] = {"any": [quota, RULE["when"]]}
    assert decide(rule, local_ledger=lambda q, n: None)
    account.assert_not_called()


def test_ordered_rules_and_invalid_signal():
    bad = deepcopy(RULE)
    bad["when"]["schedule"]["tz"] = "Invalid/Zone"
    second = deepcopy(RULE)
    second["name"] = "second"
    result = evaluate_route_policy({"route_policy": {"enabled": True, "rules": [bad, RULE, second]}},
                                   scope="gateway_chat", boundary="new_session", now=NOW)
    assert result["route_policy_rule"] == "peak"


@pytest.mark.parametrize("event", ["turn", "cache_cold"])
def test_authorized_warm_boundary_can_return_to_primary(event):
    rule = deepcopy(RULE)
    rule["apply_at"] += [event]
    rule["accept_cache_miss"] = True
    rule["cache_cold_after"] = 600
    stored = decide(rule)
    assert decide(rule, boundary="turn", current_override=stored, now=NOW.replace(hour=11), idle_seconds=600) is None
