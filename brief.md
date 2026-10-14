# Review request round 2: deterministic gateway service PATH (hermes fork)

Round 1 of this review returned REQUEST-CHANGES with 5 findings; all were fixed in commit
`22830b8022` ("review round 1 — XML-escape PATH payload, canonical system tail, hardened tests").
This round reviews the FULL updated diff: `review-02.diff` (uncommitted file at the worktree root,
`git diff 81f2815428..HEAD` is the same content — two commits).

## What changed since round 1 (verify these fixes, adversarially)

1. P1 XML injection: `generate_launchd_plist()` now XML-escapes the whole `sane_path` payload
   (`escape(...)` from `xml.sax.saxutils`, already imported). Round-trip test with a
   `tools&helpers` dir proves single-escaping and byte-stability across a regeneration that
   carries the entry forward from the installed plist.
2. P2 ordering: `_launchd_service_path` excludes canonical system dirs from the carried-forward
   segment, so the tail is appended exactly once and custom entries always precede system dirs
   (test `test_installed_system_dirs_move_to_canonical_tail`).
3. P2 malformed plist: `_installed_plist_path_dirs` validates the PATH value is a `str` inside
   the try-block; a `<integer>42</integer>` PATH fails open to no carry-forward
   (test `test_malformed_installed_plist_fails_open`).
4. P2 Linux compat: the test file now follows the repo's `@pytest.mark.platforms(...)`
   convention (see `tests/_fixtures/platform_gating.py` rationale); `sys.platform` patching
   removed; exact-list tests fully fake directory existence (`_fake_only_these_dirs_exist`)
   so they are host-independent. Linux lane runs the `platforms("linux")` tests.
5. P2 drift test: the test now rewrites ONLY the PATH value inside the generator's own output
   text (no `plistlib.dumps` re-serialization), so inequality is attributable to the PATH alone.
   Independently verified: with the old PATH-blanking normalizer restored, that drifted plist
   compares EQUAL (the old bug would pass the test's premise); with the new normalizer it
   compares UNEQUAL.

## Also verify

- Nothing else regressed: local suite 183 passed / 7 skipped across the 7 affected test files;
  ruff clean; `scripts/check_fork_features.py` exit 0.
- The carry-forward warning logic, reset-flag window in `_cmd_install`, and
  `_service_path_extra_dirs` fail-open parsing from round 1 are unchanged except where noted.

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial.
