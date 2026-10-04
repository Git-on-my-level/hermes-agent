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
]

TEST_FILES = [
    "tests/hermes_cli/test_update_configured_git_target.py",
    "tests/hermes_cli/test_converge_release_status.py",
    "tests/hermes_cli/test_sync_prod_tip.py",
    "tests/gateway/test_stream_consumer_commentary_preview.py",
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
