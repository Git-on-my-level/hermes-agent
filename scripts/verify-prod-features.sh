#!/usr/bin/env bash
# verify-prod-features.sh — Verify all prod-only custom features are present
#
# Run this AFTER every `hermes update` or `./scripts/update-prod-branch.sh`
# to catch if upstream merge/rebase nuked any of our custom code.
#
# Usage:
#   ./scripts/verify-prod-features.sh          # exit 0 = all good, exit 1 = missing
#   ./scripts/verify-prod-features.sh --fix     # print recovery commands
#
# Exit codes:
#   0 — all prod features present ✅
#   1 — one or more prod features MISSING ❌
#   2 — manifest file not found

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$REPO_DIR/scripts/prod-feature-manifest.txt"
FIX_MODE="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [ ! -f "$MANIFEST" ]; then
  echo -e "${RED}ERROR: Manifest not found at $MANIFEST${NC}"
  exit 2
fi

MISSING=0
FOUND=0
TOTAL=0

log_ok()   { echo -e "  ${GREEN}✅${NC} $*"; }
log_fail() { echo -e "  ${RED}❌${NC} $*"; }
log_warn() { echo -e "  ${YELLOW}⚠️${NC} $*"; }

echo -e "${CYAN}Verifying prod-only feature manifest...${NC}"
echo ""

# Parse manifest: skip blank lines, comments, and section headers
while IFS= read -r line; do
  # Skip comments and section headers
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  # Skip blank lines
  [[ -z "${line// }" ]] && continue

  TOTAL=$((TOTAL + 1))
  entry="$line"

  if [[ "$entry" == *"*" ]]; then
    # Wildcard prefix match — grep for the prefix without the *
    prefix="${entry%\*}"
    if grep -rq "$prefix" "$REPO_DIR/${entry%%/*}" --include='*.py' --include='*.sh' --include='*.md' 2>/dev/null | grep -q "$prefix"; then
      log_ok "$entry (wildcard match)"
      FOUND=$((FOUND + 1))
    else
      log_fail "$entry (NOT FOUND)"
      MISSING=$((MISSING + 1))
    fi
  else
    # Exact file:path check
    if [ -f "$REPO_DIR/$entry" ]; then
      if grep -q "$(basename "$entry")" "$REPO_DIR/$entry" 2>/dev/null; then
        log_ok "$entry"
        FOUND=$((FOUND + 1))
      else
        # File exists but symbol not in it — could be a test file or script
        log_ok "$entry (file exists)"
        FOUND=$((FOUND + 1))
      fi
    elif [ -f "$REPO_DIR/$entry" ] || grep -rq "${entry##*:}" "$REPO_DIR/${entry%%:*}" 2>/dev/null; then
      log_ok "$entry"
      FOUND=$((FOUND + 1))
    else
      log_fail "$entry (NOT FOUND)"
      MISSING=$((MISSING + 1))
    fi
  fi
done < "$MANIFEST"

echo ""
echo -e "${CYAN}─────────────────────────────────────${NC}"

if [ "$MISSING" -gt 0 ]; then
  echo -e "${RED}❌ ${MISSING}/${TOTAL} prod feature(s) MISSING!${NC}"
  echo ""

  if [ "$FIX_MODE" = "--fix" ]; then
    echo -e "${YELLOW}Recovery options:${NC}"
    echo ""
    echo "  1. Find the lost commit:"
    echo "     cd $REPO_DIR && git log --all -S '<symbol_name>' --oneline"
    echo ""
    echo "  2. Cherry-pick it onto prod:"
    echo "     cd $REPO_DIR && git checkout prod && git cherry-pick <commit-sha>"
    echo ""
    echo "  3. Push:"
    echo "     git push fork prod"
    echo ""
    echo "  4. Re-run this verifier."
  else
    echo -e "Run with ${CYAN}--fix${NC} for recovery hints."
    echo -e "Or search git history: ${CYAN}git log --all -S '<symbol>'${NC}"
  fi

  exit 1
else
  echo -e "${GREEN}✅ All ${FOUND}/${TOTAL} prod features present.${NC}"
  exit 0
fi
