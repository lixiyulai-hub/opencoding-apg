#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
for path in \
  "scripts/apg_beginner_executor_preview.py" \
  "tests/test_apg_beginner_executor.py" \
  "docs/apg/APG_BEGINNER_EXECUTOR_OFFLINE_CONTRACT.md" \
  "README.md" \
  "README_CN.md"; do
  cp -- "$PREIMAGE/$path" "$TARGET/$path"
done
for path in \
  "docs/apg/APG_BEGINNER_CAPABILITY_MATRIX.md" \
  "docs/apg/APG_BEGINNER_DYNAMIC_DOCUMENT_PACKAGE.md" \
  "docs/apg/APG_BEGINNER_TASK_WAVES.md"; do
  rm -f -- "$TARGET/$path"
done
rm -rf -- "$TARGET/artifacts/opencoding-beginner-orchestration-20260904"
printf '%s\n' "rollback-restored; governance receipts, snapshots, Ledger, AGENTS.md, and unrelated changes preserved"
