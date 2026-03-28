#!/usr/bin/env bash
set -euo pipefail

# Maintainer workflow for the personal Hermes fork:
# 1. sync local/fork main to upstream origin/main
# 2. merge updated main into prod
# 3. push fork/prod

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "→ Fetching remotes..."
git fetch origin --prune
git fetch fork --prune

echo "→ Syncing main to upstream origin/main..."
git checkout main
git reset --hard origin/main
git push fork main:main --force-with-lease

echo "→ Refreshing prod from fork/prod..."
if git show-ref --verify --quiet refs/heads/prod; then
  git checkout prod
else
  git checkout -b prod --track fork/prod
fi
git reset --hard fork/prod

echo "→ Merging main into prod..."
git merge --no-edit main

echo "→ Pushing prod..."
git push fork prod

echo
echo "✓ fork/main now mirrors upstream and fork/prod now includes the merged updates."
