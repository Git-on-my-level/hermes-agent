#!/usr/bin/env python3
"""Keep-list guard: assert the fork's carried features survive an upstream sync.

Run after every upstream sync, before pushing fork/prod:
    python3 scripts/check_fork_features.py

Exit 0 = every keep-list item is present; exit 1 = one clear line per
missing item. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (relative path, required substring, human label)
KEEP_LIST: list[tuple[str, str, str]] = [
    (
        "tests/gateway/test_auto_start_goal.py",
        "def test_auto_start_and_inference_share_one_goal_loop(",
        "NousResearch/hermes-agent#114921 enabled explicit/paused precedence regression",
    ),
    (
        "tests/gateway/test_goal_autodrive_regressions.py",
        "def test_goal_busy_correction_replaces_automatic_objective(",
        "NousResearch/hermes-agent#114921 busy correction regression",
    ),
    (
        "tests/gateway/test_goal_autodrive_regressions.py",
        "def test_goal_auto_start_replacement_clears_old_continuations(",
        "NousResearch/hermes-agent#114921 obsolete continuation cleanup regression",
    ),
    (
        "tests/gateway/test_goal_autodrive_regressions.py",
        "def test_goal_kickoff_is_synthetic_and_preserves_contract(",
        "NousResearch/hermes-agent#114921 command kickoff provenance regression",
    ),
    (
        "tests/gateway/test_goal_autodrive_regressions.py",
        "def test_goal_gate_continuation_preserves_budget_and_pause_dequeues_it(",
        "NousResearch/hermes-agent#114921 quality-gate continuation regression",
    ),
    (
        "tests/gateway/test_goal_autodrive_regressions.py",
        "def test_goal_enabled_lifecycle_preserves_state_and_single_continuation(",
        "NousResearch/hermes-agent#134448 enabled notification/correction/blocked lifecycle regression",
    ),
    (
        "tests/hermes_cli/test_goal_continuation_actions.py",
        "def test_goal_continuation_requires_action_with_all_criteria(",
        "NousResearch/hermes-agent#129380 contract/subgoal action regression",
    ),
    (
        "tests/hermes_cli/test_goal_continuation_actions.py",
        "def test_auto_infer_false_string_does_not_call_judge(",
        "NousResearch/hermes-agent#134448 false-string opt-in regression",
    ),
    (
        "hermes_cli/goals.py",
        "def maybe_infer_goal",
        "NousResearch/hermes-agent#134448 commitment-based goal inference",
    ),
    (
        "hermes_cli/config_defaults.py",
        '"auto_infer": False',
        "NousResearch/hermes-agent#134448 inference stays opt-in",
    ),
    (
        "gateway/run_goals.py",
        "lambda: maybe_infer_goal(mgr, last_user, final_response",
        "NousResearch/hermes-agent#134448 gateway inference hook",
    ),
    (
        "hermes_cli/cli_loops_mixin.py",
        "notice = maybe_infer_goal(mgr, last_user, reply)",
        "NousResearch/hermes-agent#134448 CLI inference hook",
    ),
    (
        "gateway/run_inbound.py",
        "await self._auto_start_goal_for_inbound_event(event)",
        "NousResearch/hermes-agent#114921 inbound automatic goal hook",
    ),
    (
        "gateway/run_goals.py",
        "or not self._turn_is_user_authored(event)",
        "NousResearch/hermes-agent#114921 automatic goals exclude synthetic events",
    ),
    (
        "hermes_cli/config_defaults.py",
        '"auto_start": False',
        "NousResearch/hermes-agent#114921 automatic gateway goals stay opt-in",
    ),
    (
        "hermes_cli/goals.py",
        "tool to take one concrete step before replying.",
        "NousResearch/hermes-agent#129380 actionable standing-goal continuation",
    ),
    (
        "hermes_cli/goals.py",
        "def gather_tool_activity",
        "NousResearch/hermes-agent#117222 host-observed tool evidence extraction",
    ),
    (
        "hermes_cli/goals.py",
        "tool_activity=gather_tool_activity(self.session_id)",
        "NousResearch/hermes-agent#117222 tool evidence reaches the goal judge",
    ),
    ("plugins/goal-heartbeat/__init__.py", "def candidates(", "fork plugin: goal-heartbeat idle-goal check-ins"),
    (
        "tests/plugins/test_goal_heartbeat_plugin.py",
        "def test_count_escalates_then_stops_until_a_real_event(",
        "fork plugin: goal-heartbeat escalation regression",
    ),
    (
        "gateway/run_goals.py",
        "if quiet_internal and await self._run_in_executor_with_context(mgr.wait_barrier_live):",
        "fork: silent internal turns do not judge a goal whose wait still holds",
    ),
    (
        "tests/gateway/test_goal_silent_internal_turn.py",
        "def test_quiet_turn_after_the_barrier_lifted_is_still_judged(",
        "fork: silent internal turn regression",
    ),
    ("hermes_cli/goals.py", "def wait_barrier_live(self)", "fork: read-only wait-barrier liveness"),
    (
        "tests/e2e/test_goal_heartbeat_e2e.py",
        "def test_silent_heartbeat_keeps_live_park_and_judges_once_the_process_exits(",
        "fork: e2e silent heartbeat through the real gateway path",
    ),
    ("plugins/session-inject/__init__.py", "def confirm_sent(", "fork plugin: session-inject delivery confirmation"),
    ("plugins/session-inject/__init__.py", "def drain_once(", "fork plugin: session-inject spool drain"),
    ("plugins/session-inject/cli.py", "def queue(", "fork plugin: `hermes inject` CLI"),
    (
        "tests/plugins/test_session_inject_plugin.py",
        "def test_concurrent_drains_dispatch_each_request_exactly_once(",
        "fork plugin: session-inject exactly-once regression",
    ),
    ("hermes_cli/update_git_target.py", "def configured_git_target", "presence-sensitive git deploy target"),
    ("hermes_cli/update_cmd.py", "configured_git_target(args)", "updater selects configured git target"),
    ("hermes_cli/subcommands/converge.py", "--assert-current", "checkout/live release assertion"),
    ("scripts/check_sync_prod_tip.py", "check_prod_tip", "pre-land prod drift check"),
    (
        "gateway/stream_consumer_preview.py",
        "StreamCommentaryPreviewMixin",
        "commentary preview mixin class",
    ),
    (
        "gateway/stream_consumer_preview.py",
        "_commentary_preview_edit_supported = True",
        "commentary preview edit re-enable after degraded fresh send",
    ),
    (
        "gateway/commentary_preview.py",
        "def telegram_preview_channel",
        "telegram preview channel resolver",
    ),
    (
        "gateway/run_turn_runner.py",
        "telegram_preview_channel",
        "run_turn_runner plumb-through of telegram_preview_channel",
    ),
    (
        "gateway/stream_consumer.py",
        "commentary_mode",
        "stream_consumer commentary_mode config plumb-through",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        "_message_thread_id_for_send(self._metadata_thread_id(metadata))",
        "telegram send lock keyed by chat plus topic (General topic 1 shares the bare chat FIFO)",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        'key = f"{key}:{thread_id}"',
        "telegram send lock chat-plus-topic key construction",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        "async with self._chat_send_lock(chat_id, metadata):",
        "telegram send() takes the topic-scoped chat send lock",
    ),
    (
        ".github/workflows/fork-ci.yml",
        "ubuntu-latest",
        "fork CI uses standard runners (not 96-core)",
    ),
    (
        "scripts/fork_ci_apply.sh",
        "gh workflow disable",
        "post-sync script disables upstream-oriented workflows",
    ),
    (
        "scripts/prune_fork_branches.py",
        "--force-with-lease",
        "guarded branch deletion for the retention apply pass",
    ),
    (
        "AGENTS.md",
        "FORK.md",
        "AGENTS.md points at the fork maintenance rules",
    ),
    (
        "hermes_cli/update_channel.py",
        "def is_stock_upstream_probe",
        "banner behind-count skips the official origin/main shortcut off stock",
    ),
    (
        "hermes_cli/source_check.py",
        "def _check_configured_git_ref",
        "configured updates.remote/branch behind-count",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        "is_near_split",
        "telegram inbound UTF-16 split helper",
    ),
    (
        "agent/agent_init.py",
        "skip_tool_search_assembly=_xai_responses",
        "xAI Responses does not register the tool_search bridge",
    ),
    (
        "hermes_cli/cron.py",
        "Cannot set --model or --provider on a no-agent job",
        "cron refuses model/provider pins on no_agent jobs",
    ),
    (
        "agent/conversation_compression.py",
        "def reset_ui_delivery_state_after_compaction",
        "compaction re-opens mid-turn commentary delivery",
    ),
    (
        "hermes_cli/update_converge.py",
        "def cmd_converge_tick",
        "idle converge tick",
    ),
    (
        "hermes_cli/config_defaults.py",
        '"skip_gateway_restart"',
        "updates.skip_gateway_restart converge default",
    ),
    (
        "gateway/run_notifications.py",
        "Planned-restart online notice skipped: quiet drain restart",
        "quiet drain skips the gateway-online broadcast",
    ),
    (
        "gateway/run_shutdown.py",
        "Home-channel shutdown broadcast suppressed: external supervisor recycle",
        "launchd recycle skips the home-channel shutdown broadcast",
    ),
    (
        "gateway/drain_control.py",
        "def external_supervisor_shutdown_is_quiet",
        "external supervisor shutdown quiet probe",
    ),
    (
        "hermes_cli/gateway_launchd.py",
        "<key>HardResourceLimits</key>",
        "launchd nofile hard ceiling",
    ),
    (
        "hermes_cli/gateway_launchd.py",
        "def _launchctl_domain_supervising_process",
        "domain-scoped launchd supervision probe",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        "def _media_collect_token",
        "telegram album sibling hold",
    ),
    (
        "gateway/display_config.py",
        '"interim_assistant_message_mode": "separate"',
        "interim_assistant_message_mode default registration",
    ),
    (
        "gateway/display_config.py",
        '_norm_choice(("separate", "preview"))',
        "interim_assistant_message_mode normaliser",
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        "silent commentary scatter",
        "telegram edit_message delegates to the replacement adapter",
    ),
    (
        "gateway/stream_consumer_preview.py",
        "Commentary preview edit still failing; not sending another bubble",
        "persistent commentary edit failure does not mint a bubble per item",
    ),
]

TEST_FILES = [
    "tests/gateway/test_goal_autodrive_regressions.py",
    "tests/hermes_cli/test_goal_continuation_actions.py",
    "tests/hermes_cli/test_goal_auto_infer.py",
    "tests/gateway/test_auto_start_goal.py",
    "tests/hermes_cli/test_goals.py",
    "tests/hermes_cli/test_goal_gates.py",
    "tests/hermes_cli/test_update_configured_git_target.py",
    "tests/hermes_cli/test_converge_release_status.py",
    "tests/scripts/test_sync_prod_tip.py",
    "tests/gateway/test_stream_consumer_commentary_preview.py",
    "tests/gateway/test_interim_assistant_message_mode.py",
    "tests/gateway/test_telegram_edit_replacement.py",
    "tests/gateway/test_telegram_topic_scoped_send_lock.py",
    "tests/scripts/test_prune_fork_branches.py",
    "tests/gateway/test_telegram_inbound_split.py",
    "tests/hermes_cli/test_update_channel_config.py",
    "tests/agent/test_xai_tool_search_registration.py",
    "tests/agent/test_reset_ui_delivery_after_compaction.py",
    "tests/gateway/test_rearm_typing_after_compaction.py",
    "tests/hermes_cli/test_update_converge.py",
]


def main() -> int:
    missing: list[str] = []
    for rel, needle, label in KEEP_LIST:
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"MISSING FILE: {rel} ({label})")
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if needle not in text:
            missing.append(f"MISSING {label}: '{needle}' not found in {rel}")
    for rel in TEST_FILES:
        if not (REPO_ROOT / rel).is_file():
            missing.append(f"MISSING FILE: {rel} (contract tests)")

    if missing:
        for line in missing:
            print(line)
        return 1
    print(
        f"OK: fork keep-list intact ({len(KEEP_LIST)} code checks, {len(TEST_FILES)} test file)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
