# Review request round 5: deterministic gateway service PATH (hermes fork)

Rounds 1-4 returned REQUEST-CHANGES; every finding was fixed (commits 22830b8022, d6459c11fb,
ab4fd42363, 5ae28ed21c). This round reviews the FULL updated diff: `review-05.diff` (uncommitted
file at the worktree root; `git diff 81f2815428..HEAD` — five commits).

## Fixed since round 4 (verify adversarially)

1. Double-leading-slash: `_normalize_service_path_entry` collapses the redundant leading slashes
   normpath keeps (`//usr/bin` → `/usr/bin`), so `//usr/bin/` deduplicates against the canonical
   dir in both generators, head and carry-forward
   (`test_double_leading_slash_spellings_bubbles_to_tail`).
2. System units read the TARGET user's config: `generate_systemd_unit(system=True)` now wraps
   `_build_service_path_dirs()` in the target-home override (set/reset, mirroring
   `_systemd_watchdog_seconds`), so `service_path_extra` resolves from the target user's
   hermes home / config, never the sudo installer's
   (`test_system_unit_reads_target_users_config_extras`: two distinct homes through the real
   loader seam keyed on `get_hermes_home()`).
3. The relative-extra round-trip test now points `get_launchd_plist_path` at the installed
   plist before regenerating and asserts `launchd_plist_is_current() is True` — it exercises
   the real carry-forward path instead of two fresh generations.

## Standing verification state

- Local: 192 passed / 7 skipped across the 7 affected test files; ruff clean;
  `scripts/check_fork_features.py` exit 0.
- All prior-round fixes remain in place and tested (XML escape round-trip, malformed plist
  fail-open, canonical tail over all segments incl. trailing-slash spellings, workdir-anchored
  carry-forward, final-PATH warnings, platform gating, PATH-only drift falsification,
  reset-flag window).

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial.
