# Review request round 4: deterministic gateway service PATH (hermes fork)

Rounds 1-3 returned REQUEST-CHANGES; every finding was fixed (commits 22830b8022, d6459c11fb,
ab4fd42363). This round reviews the FULL updated diff: `review-04.diff` (uncommitted file at the
worktree root; `git diff 81f2815428..HEAD` is the same content — four commits).

## Fixed since round 3 (verify adversarially)

1. Lexical normalization: `_normalize_service_path_entry` (os.path.normpath) is applied before
   every dedupe and canonical-system comparison in BOTH generators — configured extras,
   carried-forward entries, head entries, and the tail merge. `/usr/bin/` can no longer bypass
   the tail filter (`test_equivalent_system_dir_spellings_bubbles_to_tail`).
2. systemd head strip: the unit now runs the shared `_strip_canonical_system_dirs` over the
   assembled head before appending the tail, so `service_path_extra: [/usr/bin/, custom]` puts
   custom first there too (`test_unit_config_system_dir_bubbles_to_tail`).
3. Relative config extras: `service_path_extra` entries are `~`-expanded, anchored to
   `_stable_service_working_dir()` when relative, and normalized at generation time — the
   installed absolute spelling round-trips byte-identically with no phantom refresh
   (`test_relative_config_extra_anchors_and_stabilizes`).

## Standing verification state

- Local: 190 passed / 7 skipped across the 7 affected test files; ruff clean;
  `scripts/check_fork_features.py` exit 0.
- All round 1/2 fixes still in place and still tested (XML escape round-trip, malformed plist
  fail-open, workdir-anchored carry-forward, final-PATH warning computation, platform gating,
  PATH-only drift test with old-normalizer falsification).

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial.
