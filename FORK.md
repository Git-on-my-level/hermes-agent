# Fork maintenance

These rules are mandatory for source, sync, and deployment work. They make the
Hermes fork reviewable; they do **not** authorize a sync, deployment, or gateway
restart.

## Remote and overlay contract

The maintained runtime checkout uses these names deliberately:

- `origin` is the NousResearch `hermes-agent` upstream.
- `fork` is the authoritative `Git-on-my-level/hermes-agent` fork.

Do not rename remotes ad hoc. Existing automation and worktrees depend on this
convention.

Keep generic core changes upstreamable and minimal. Host-specific overlays
belong outside the repository under `~/.hermes` and macOS LaunchAgents. Never
commit credentials, bot tokens, config, sessions, logs, skills, cron state, or
installed-source state to Git.

**Thin fork content.** `fork/prod` is exact `origin/main` plus a handful of
small additive commits. Product defaults (model slug, thinking effort, silent
catalog default) live in host `config.yaml`, not in `hermes_cli/models.py`.

**Do not edit files upstream rewrites weekly** unless there is no other hook:

- `gateway/run.py`, `hermes_cli/models.py`, `hermes_cli/update_cmd.py`,
  `website/static/api/model-catalog.json`

Prefer: new file + one call site, transport-only alias, or host config.

Do not add core model tools on the fork (`toolsets.py` / conversation loop).
Use a skill or slash command until upstream owns the tool.

## Before any sync or source integration

1. Start with a clean worktree: `git status --short` must be empty. Never
   mutate the live gateway checkout (`~/.hermes/hermes-agent`); use
   `/Volumes/scratch/worktrees/…`.
2. Record exact SHAs and divergence, not branch labels alone:

   ```bash
   git fetch origin main && git fetch fork prod main
   git rev-parse origin/main fork/main fork/prod
   git rev-list --left-right --count fork/prod...origin/main
   git merge-tree --write-tree --messages fork/prod origin/main
   ```

3. Use `sync/upstream-YYYY-MM-DD` from **current `origin/main`**, then
   cherry-pick only the keep-list. Do not merge 2k-commit histories.
4. Do not commit directly to `fork/main`. Never use a blind `git pull`,
   `git fetch --all --tags`, a destructive reset, or automatic conflict
   resolution. Archive tips before any force-with-lease:

   ```bash
   git push fork fork/prod:refs/heads/archive/prod-pre-sync-YYYY-MM-DD
   git push fork fork/main:refs/heads/archive/main-pre-sync-YYYY-MM-DD
   ```

5. Retention is part of the same sitting, not a someday cleanup — see
   § Branch retention. Pushing the sync N snapshot pair is what makes the
   pair from sync N−2 eligible to go; run the prune pass before closing the
   sync.
6. Sync weekly. A 10-day lag is thousands of commits but still ~20 conflict
   files; waiting does not make `run.py` easier.

## Keep-list policy

Re-port a fork commit only if all hold:

1. Upstream still lacks the behavior.
2. The delta is a new file or a transport/platform-local hook.
3. A focused test fails if the behavior regresses.

Drop or move to `~/.hermes` when upstream landed an equivalent (GLM-5.3
catalog, mcp 2.x HTTP, patient Z.AI 429s, curated-before-fuzzy).

## Keep-list check

After every upstream sync, before pushing `fork/prod`, run:

```bash
python3 scripts/check_fork_features.py
```

It asserts each keep-list file/symbol is still present (commentary preview
mixin, resolver, plumb-throughs, contract tests). Exit 0 = intact; exit 1
prints one line per missing item — re-port it before pushing.

## Branch retention

Every sync added branches and nothing removed them: the fork reached 403
remote branches, 92% of them with a tip older than 90 days (#36). The count
only rises unless deletion is a step someone runs.

`scripts/prune_fork_branches.py` keeps a branch only if one of these holds:

1. It is `main` or `prod`, or the remote's default branch.
2. It is the head of an open PR — fork-internal or fork → upstream.
3. Its tip is newer than 90 days (the working set, and the escape hatch for
   in-flight work).
4. It is one of the newest 2 `archive/<channel>-pre-sync-<date>` dates —
   enough to roll back the sync that just happened and the one before it.

Rule 4 is date-only on purpose: a pre-sync snapshot's tip is just an old
position of `main`/`prod`, so it is always as fresh as the sync that made it —
if the 90-day rule could rescue snapshots, the archive set would grow forever.
Manual archives (`archive/*-pre-sync-*` names excluded) are ordinary branches
under rule 3.

Everything else is deleted, in two passes — never one:

```bash
# Pass 1: fetch, evaluate the rules, write the delete list (name, tip SHA,
# tip date). Commit it: the committed list is the recovery record —
# `git push fork <sha>:refs/heads/<name>` resurrects any ref in it.
python3 scripts/prune_fork_branches.py
git add scripts/prune-lists/ && git commit -m "chore: fork branch prune list YYYY-MM-DD"

# Pass 2: delete exactly what the committed list names. Every delete is
# guarded by --force-with-lease=<ref>:<sha>, so a branch that moved since
# the list was published is rejected, not deleted; channel branches and open
# PR heads are re-checked at apply time and skipped.
python3 scripts/prune_fork_branches.py --apply --list-file scripts/prune-lists/YYYY-MM-DD.txt
```

Before pass 2, confirm no host tracks a branch outside the keep list — that
is the one deletion that would be user-visible. Runtime hosts pin
`updates.branch: prod` (§ Deploy channel); `hermes update --check` on a host
is the cheap confirmation.

Run pass 1 quarterly even when no sync happened: rule 3 moves, so yesterday's
fresh branch eventually becomes eligible. If a committed list has gone stale
by apply time, pass 2 reports the moved refs and exits non-zero — regenerate
rather than forcing.

## Deploy channel (`hermes update` / `/update`)

Runtime agents on this fork should track the reviewed deploy tip, not raw
upstream `main`:

```yaml
# ~/.hermes/config.yaml  (host-local)
updates:
  remote: fork
  branch: prod
```

With that set, `hermes update`, `hermes update --check`, and `/update` all
fast-forward `fork/prod`. Upstream synchronization into the fork remains a
separate maintainer step; agents never merge upstream themselves.

Override for one shot: `hermes update --remote origin --branch main`.

## Fork CI (keep-list)

Upstream `ci.yaml` orchestrates 96-core Linux, Windows, macOS, nix, docker,
and docs lanes. This fork does not have those runners — PRs sat queued on
`ubuntu-latest-96-core` indefinitely.

Do **not** patch `ci.yaml` (upstream rewrites it weekly). Instead:

- Additive workflow: `.github/workflows/fork-ci.yml` (ruff + Linux pytest on
  `ubuntu-latest`). No-op on `NousResearch/hermes-agent`.
- GitHub-side disable of every other workflow, applied by
  `scripts/fork_ci_apply.sh` (idempotent; re-run after each upstream sync
  because GitHub auto-enables newly added workflow files).

```bash
scripts/fork_ci_apply.sh
```

Keep-list files: `fork-ci.yml`, `scripts/fork_ci_apply.sh`. Sync conflict
surface is zero against `ci.yaml`.

## Documentation verification

```bash
test -f FORK.md && rg -F 'FORK.md' AGENTS.md && git diff --check
```
