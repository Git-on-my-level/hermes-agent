# Review request round 3: deterministic gateway service PATH (hermes fork)

Rounds 1 and 2 returned REQUEST-CHANGES; every finding was fixed (commits 22830b8022, d6459c11fb).
This round reviews the FULL updated diff: `review-03.diff` (uncommitted file at the worktree root,
`git diff 81f2815428..HEAD` is the same content — three commits).

## Fixed since round 2 (verify adversarially)

1. Config system dirs bypassing the tail: canonical system dirs are now stripped from EVERY
   mid-PATH segment — including the tool/config head, so `service_path_extra: [/usr/bin, ...]`
   can no longer pin `/usr/bin` ahead of custom dirs — and the canonical tail is appended exactly
   once at the end (`test_config_extra_system_dir_bubbles_to_tail`).
2. systemd PATH escaping: the unit emits its PATH via the existing `_systemd_env_line`
   (backslash/quote escaping, `%` doubling) instead of a raw f-string; a
   `service_path_extra` dir like `tools"help\er%s` round-trips
   (`test_unit_path_escapes_hostile_dir_names`). NOTE: this changes the emitted line format
   from `Environment="PATH=<raw>"` to the shared escaper's output — verified byte-identical for
   ordinary paths (no quotes/backslashes/%) since escaping is a no-op there.
3. Relative carry-forward entries resolve against `_stable_service_working_dir()` (the service's
   cwd anchor), never the regenerating process's cwd
   (`test_relative_carry_forward_anchors_to_service_workdir`).
4. Dead-entry warnings are computed against the FINAL PATH after the tail is appended, so a
   healthy canonical installed PATH regenerates warning-free
   (`test_healthy_system_tail_not_reported_dead`); `--reset-service-path` logs its intentional
   removals under a separate, accurate message.

## Standing verification state

- Local: 187 passed / 7 skipped across the 7 affected test files (updated after these fixes);
  ruff clean; `scripts/check_fork_features.py` exit 0.
- Round-1/2 fixes still in place and still tested (XML escape round-trip, malformed plist
  fail-open, platform gating, PATH-only drift test with old-normalizer falsification).

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial.
