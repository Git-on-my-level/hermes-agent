#!/usr/bin/env bash
# install-githooks.sh — symlink .githooks/ into .git/hooks so they run automatically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR/..")"
HOOKS_SRC="$REPO_ROOT/.githooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_SRC" ]; then
    echo "Error: $HOOKS_SRC not found. No git hooks to install." >&2
    exit 1
fi

mkdir -p "$HOOKS_DST"

for hook in "$HOOKS_SRC"/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    target="$HOOKS_DST/$name"
    # Use symlink so edits to .githooks/ are reflected immediately.
    ln -sf "$hook" "$target"
    chmod +x "$hook"
    echo "  Installed $name"
done

echo "Git hooks installed from .githooks/"
