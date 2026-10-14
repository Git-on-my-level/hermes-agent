# Review request: deterministic gateway service PATH (hermes fork)

You are reviewing a commit in this worktree (branch `fix/gateway-service-path-determinism`).

Read the full diff first: `review-01.diff` (uncommitted file at the worktree root,
`git diff 81f2815428..HEAD` gives the same content). Then read the touched files for context:
`hermes_cli/gateway.py` (search `_service_tool_dirs`, `_build_service_path_dirs`,
`generate_systemd_unit`, `systemd_unit_is_current`, `_cmd_install`), `hermes_cli/gateway_launchd.py`
(`_launchd_service_path`, `_installed_plist_path_dirs`, `generate_launchd_plist`),
`hermes_cli/subcommands/gateway.py`, `tests/hermes_cli/test_gateway_service_path_determinism.py`.

## The bug being fixed

`generate_launchd_plist()` built the plist PATH as `node dirs + os.environ["PATH"]` of whatever
process regenerated it. The `ai.hermes.converge` auto-updater runs under launchd's minimal PATH, so
its regenerations silently dropped `/opt/homebrew/bin`, `/opt/homebrew/sbin`, `/usr/local/bin`,
`~/.local/bin` from the live gateway service (m4-mac-mini, 2026-10-14: converge overwrote a hand
fix at 07:05; same failure class that blinded prod monitors for 7 days).
`launchd_plist_is_current()` compared against that same caller-dependent output, so "is the plist
current" depended on who asked. The old comparison regex-blanked the PATH payload, which HID this
drift from the staleness check.

## The fix (what to review)

1. Service PATH is now a pure function of the host: `~/.local/bin`, existing package-manager dirs
   (`/opt/homebrew/bin`, `/opt/homebrew/sbin`, `/usr/local/bin`, `/usr/local/sbin` on macOS;
   `/usr/local/*` on Linux), new `gateway.service_path_extra` config key, managed Node dirs, then
   system dirs last. The invoking shell PATH is NEVER consulted (deliberately — a caller-supplied
   segment would make the is_current comparison caller-dependent again).
2. Regeneration carries forward installed-plist PATH entries that still exist on disk (never
   shrinks), logging a WARNING when the caller env lacks them. `gateway install
   --reset-service-path` is the operator escape hatch that re-derives without carry-forward
   (flag plumbed through `_cmd_install` via the facade global `_reset_service_path`).
3. `launchd_plist_is_current()` no longer blanks the PATH payload (deterministic by construction
   now; keeping the blanking would have left drifted plists looking "current" forever).
   `systemd_unit_is_current()` keeps its byte-exact compare; the unit never carried caller PATH.
4. Tests: `tests/hermes_cli/test_gateway_service_path_determinism.py` (22 tests) + 3 existing tests
   pinned against reading the real host plist. Full local suite green:
   181 passed / 5 skipped across the 7 affected test files; `scripts/check_fork_features.py` exit 0;
   ruff clean.

## Review focus

- Correctness of the carry-forward logic (`_launchd_service_path`): idempotence, warning conditions,
  the reset flag path, plistlib failure modes.
- The `_normalize_launchd_plist_for_comparison` change: is dropping the PATH-blank safe now, and
  does anything else rely on the old regex behavior?
- `_cmd_install`'s global-flag window (set → launchd_install → finally reset): leak paths,
  exception paths, reentrancy.
- `_service_path_extra_dirs()` config parsing (fail-open behavior).
- Test quality: do the tests actually pin the invariants (minimal-env keeps Homebrew; no-shrink;
  caller-independent is_current; reset drops carry-forward), or do they mock the thing under test?
- Any caller of `generate_launchd_plist()`/`systemd_unit_is_current()` whose behavior changes
  unexpectedly for hosts with hand-edited plists.

Output format: verdict line first (APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES), then graded
findings (severity, file:line, what, why, suggested fix). Be specific and adversarial; do not
restate the design back.
