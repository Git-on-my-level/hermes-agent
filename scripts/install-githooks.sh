#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

chmod +x .githooks/pre-push
git config core.hooksPath .githooks

printf 'Configured git hooks for %s\n' "$repo_root"
printf 'core.hooksPath=%s\n' "$(git config --get core.hooksPath)"
