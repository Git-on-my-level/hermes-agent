#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

printf '→ Fetching origin and fork...\n'
git fetch origin --prune
git fetch fork --prune

printf '→ Syncing local main to origin/main...\n'
git checkout main
git reset --hard origin/main

printf '→ Pushing fork/main mirror...\n'
git push fork main:main

printf '→ Syncing local prod to the latest remote prod branch...\n'
git fetch fork prod
if git show-ref --verify --quiet refs/heads/prod; then
  git checkout prod
else
  git checkout -B prod FETCH_HEAD
  git branch --set-upstream-to=fork/prod prod || true
fi
git reset --hard FETCH_HEAD

printf '→ Merging main into prod...\n'
git merge --no-edit main

printf '→ Pushing fork/prod...\n'
git push fork prod:prod

printf '✓ Prod branch sync complete.\n'
printf '  main: %s\n' "$(git rev-parse main)"
printf '  prod: %s\n' "$(git rev-parse prod)"
