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
        ".github/workflows/fork-ci.yml",
        "ubuntu-latest",
        "fork CI uses standard runners (not 96-core)",
    ),
    (
        "scripts/fork_ci_apply.sh",
        "gh workflow disable",
        "post-sync script disables upstream-oriented workflows",
    ),
]

TEST_FILES = [
    "tests/gateway/test_stream_consumer_commentary_preview.py",
]


def main() -> int:
    missing: list[str] = []
    for rel, needle, label in KEEP_LIST:
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"MISSING FILE: {rel} ({label})")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle not in text:
            missing.append(f"MISSING {label}: '{needle}' not found in {rel}")
    for rel in TEST_FILES:
        if not (REPO_ROOT / rel).is_file():
            missing.append(f"MISSING FILE: {rel} (contract tests)")

    if missing:
        for line in missing:
            print(line)
        return 1
    print(f"OK: fork keep-list intact ({len(KEEP_LIST)} code checks, {len(TEST_FILES)} test file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
