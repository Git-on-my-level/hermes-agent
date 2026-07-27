# Fork maintenance

These rules are mandatory for source, sync, and deployment work. They make the
Hermes fork reviewable; they do **not** authorize a sync, deployment, or gateway
restart.

## Remote and overlay contract

The maintained runtime checkout uses these names deliberately:

- `origin` is the NousResearch `hermes-agent` upstream.
- `fork` is the authoritative `Git-on-my-level/hermes-agent` fork.

Do not rename remotes ad hoc. Existing automation and worktrees depend on this
convention. A task worktree can be provisioned with a different local remote
view; treat that as a reason to inspect and reconcile the runtime checkout
before any sync, never as permission to rename or rewrite remotes.

Keep generic core changes upstreamable and minimal. M4-specific operational
overlays belong outside the repository under `~/.hermes` and macOS
LaunchAgents. Never commit credentials, bot tokens, config, sessions, logs,
skills, cron state, or installed-source state to Git.

## Before any sync or source integration

1. Start with a clean worktree: `git status --short` must be empty.
2. Record exact SHAs and divergence, not branch labels alone:

   ```bash
   git rev-parse origin/main fork/main
   git rev-list --left-right --count fork/main...origin/main
   git log --oneline fork/main..origin/main
   ```

3. Use a descriptive branch: `sync/upstream-YYYY-MM-DD` for one upstream
   synchronization, or `fix/<topic>` for one generic bug fix. Make
   one-purpose commits only.
4. Do not commit directly to `fork/main`; do not force-push or rewrite
   `fork/main`. Never use a blind `git pull`, `git fetch --all --tags`, a
   destructive reset, or automatic conflict resolution.

The observed 12,911-commit `fork/main` gap to `origin/main` requires a planned,
reviewed sync lifecycle. It is not a background merge and is not solved by this
document.

## Reviewed upstream synchronization

First inspect `scripts/update-prod-branch.sh`; it is the existing additive
sync primitive, not a fire-and-forget command. Its declared model is
`origin/main` as upstream, `fork/main` as the clean mirror, and `fork/prod` as
the integration branch. It performs explicit per-remote fetches, rebases local
`main`, may rescue `fork/prod`-only commits, merges into `prod`, and pushes.
Therefore run it only in a dedicated reviewed lifecycle after an exact-SHA
preflight and a deliberate dry-run/review; never from a feature worktree or as
an unattended repair.

For a large sync:

1. Record the preflight SHAs and left/right divergence in the review record.
2. Create `sync/upstream-YYYY-MM-DD` from the reviewed `fork/main` baseline.
3. Bring in one upstream synchronization only. Resolve conflicts manually;
   keep feature work out of the sync branch.
4. Run the relevant tests, inspect the resulting graph/diff and retained
   fork-specific behavior, then obtain review before any fork or deployment
   update.
5. Verify the reviewed merge result and exact deployed SHA before deployment.

## Generic fixes and local integration

Port or rebase a generic bug fix onto its intended current baseline first, then
test it and offer it upstream/fork as a small isolated change. Do not blindly
cherry-pick a patch made from another baseline. Local integration waits for
exact-head review. The native delivery reliability work is generic core
behavior, not an M4-only fork feature.

## Repeatable stale check

Before starting source work, repeat the preflight commands above, inspect
`git status --short`, compare the intended runtime branch with `fork/main`,
and read `scripts/update-prod-branch.sh` before proposing a sync. Escalate when
the divergence is material, the remote convention differs, or the intended
baseline is unclear; do not repair it in the background.

## Documentation verification

Run this from the repository root after editing these guardrails:

```bash
test -f FORK.md && rg -F 'FORK.md' AGENTS.md && git diff --check
```
