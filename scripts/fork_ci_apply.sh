#!/usr/bin/env bash
# Disable upstream-oriented GitHub Actions on Git-on-my-level/hermes-agent.
# Idempotent. Does not edit workflow files (those stay identical to upstream
# so weekly syncs do not conflict). New workflow files GitHub auto-enables
# after a sync — re-run this script as the last sync step.
#
# Usage: scripts/fork_ci_apply.sh
set -euo pipefail

REPO="${FORK_CI_REPO:-Git-on-my-level/hermes-agent}"

# Workflow *names* (gh workflow list first column) that may stay enabled.
# Everything else with an independent trigger is disabled. Reusable
# (workflow_call-only) workflows are left enabled so Fork CI can call Lint.
ALLOW_REGEX='^(Fork CI|Lint \(ruff \+ ty\))$'

if ! command -v gh >/dev/null; then
  echo "gh CLI required" >&2
  exit 1
fi

disabled=0
kept=0
while IFS=$'\t' read -r name state id; do
  [ -n "$name" ] || continue
  if printf '%s' "$name" | grep -Eq "$ALLOW_REGEX"; then
    echo "keep    $name ($state)"
    kept=$((kept + 1))
    continue
  fi
  if [ "$state" = "disabled_manually" ] || [ "$state" = "disabled_inactivity" ]; then
    echo "already $name"
    continue
  fi
  echo "disable $name ($id)"
  gh workflow disable "$id" --repo "$REPO"
  disabled=$((disabled + 1))
done < <(gh workflow list --repo "$REPO" --all --limit 100)

echo "done: disabled=$disabled kept=$kept repo=$REPO"
