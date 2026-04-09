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

printf '→ Checking prod patch count...\n'
prod_patches=$(git log --oneline origin/main..fork/prod 2>/dev/null | wc -l | tr -d ' ')
behind_count=$(git rev-list --count fork/prod..origin/main 2>/dev/null || echo "0")
printf '  prod patches: %s, upstream commits behind: %s\n' "$prod_patches" "$behind_count"

if [ "$behind_count" -gt 100 ] && [ "$prod_patches" -gt 20 ]; then
  printf '⚠ Large drift detected (%s upstream commits, %s prod patches).\n' "$behind_count" "$prod_patches"
  printf '  Rebase may produce many conflicts. Consider a clean reset approach.\n'
  printf '  See hermes-fork-prod skill for instructions.\n'
  printf '  Continue with rebase anyway? [y/N] '
  read -r answer
  if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
    printf 'Aborted.\n'
    exit 1
  fi
fi

printf '→ Rebasing prod onto main...\n'
git checkout prod
git rebase main || {
  printf '\n⚠ Rebase paused with conflicts. Resolve them, then:\n'
  printf '  git add <resolved-files>'
  printf '  git rebase --continue\n'
  printf '  (or: git rebase --abort to give up)\n'
  exit 1
}

printf '→ Force-pushing fork/prod...\n'
git push fork prod --force-with-lease

printf '✓ Prod branch sync complete.\n'
printf '  main: %s\n' "$(git rev-parse main)"
printf '  prod: %s\n' "$(git rev-parse prod)"
printf '  prod patches on top of main: %s\n' "$(git log --oneline origin/main..HEAD | wc -l | tr -d ' ')"
