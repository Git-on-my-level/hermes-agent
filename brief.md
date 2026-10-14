# Review request round 6: deterministic gateway service PATH (hermes fork)

Rounds 1-5 returned REQUEST-CHANGES; every finding was fixed (commits 22830b8022, d6459c11fb,
ab4fd42363, 5ae28ed21c, 7872bf7ee5). Full updated diff: `review-06.diff` (uncommitted file at the
worktree root; `git diff 81f2815428..HEAD` — six commits).

## Fixed since round 5 (verify adversarially)

1. `..` symlink semantics: carried-forward and configured entries containing `..` resolve via
   `os.path.realpath` through the shared `_resolve_service_path_entry()` — `link/../bin` with
   `link -> actual/vX.Y` keeps meaning `actual/bin` instead of lexically collapsing to the
   nonexistent sibling `tmp/bin` (which dropped the real dir and warned it dead). Entries
   without `..` keep their exact spelling so regeneration reads back and dedupes the same
   string (`test_dotdot_carry_forward_keeps_symlink_semantics`).
2. Target-user OS home: `generate_systemd_unit(system=True)` passes
   `_build_service_path_dirs(home=Path(home_dir))` — the `~/.local/bin` existence probe and
   `~` expansion of config extras run against the TARGET account, not the installer's
   `Path.home()`/`$HOME` under sudo
   (`test_system_unit_probes_target_users_local_bin`).

## Standing verification state

- Local: 194 passed / 7 skipped across the 7 affected test files; ruff clean;
  `scripts/check_fork_features.py` exit 0.
- All prior-round fixes remain in place and tested.

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial.
