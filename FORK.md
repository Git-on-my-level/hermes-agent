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

5. Sync weekly. A 10-day lag is thousands of commits but still ~20 conflict
   files; waiting does not make `run.py` easier.

## Keep-list policy

Re-port a fork commit only if all hold:

1. Upstream still lacks the behavior.
2. The delta is a new file or a transport/platform-local hook.
3. A focused test fails if the behavior regresses.

Drop or move to `~/.hermes` when upstream landed an equivalent (GLM-5.3
catalog, mcp 2.x HTTP, patient Z.AI 429s, curated-before-fuzzy).

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

## Documentation verification

```bash
test -f FORK.md && rg -F 'FORK.md' AGENTS.md && git diff --check
```
