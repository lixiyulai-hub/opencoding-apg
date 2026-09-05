#!/usr/bin/env sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: ROLLBACK.sh <preimage-dir> <target-root>" >&2
  exit 2
fi
PREIMAGE_DIR=$1
TARGET_ROOT=$2
mkdir -p "$TARGET_ROOT/.governance/progress"
cp -- "$PREIMAGE_DIR/AGENTS.md" "$TARGET_ROOT/AGENTS.md"
cp -- "$PREIMAGE_DIR/active.json" "$TARGET_ROOT/.governance/progress/active.json"
rm -f -- "$TARGET_ROOT/docs/apg/APG_AUTONOMOUS_DRIFT_REPAIR.md"
rm -f -- "$TARGET_ROOT/scripts/apg_independent_review.py"
rm -f -- "$TARGET_ROOT/tests/test_apg_independent_review.py"
rm -rf -- "$TARGET_ROOT/artifacts/apg-autonomous-drift-repair"
printf '%s\n' "rollback-restored"
