#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
rm -f -- "$TARGET/docs/apg/APG_LOCAL_GIT_CHECKPOINT_TRANSACTION_PREVIEW.md"
rm -rf -- "$TARGET/artifacts/apg-local-git-checkpoint-preview-20260903"
if [ -n "$PREIMAGE" ] && [ -f "$PREIMAGE/.governance/receipts/sentinel-preserved.json" ]; then
  test -f "$TARGET/.governance/receipts/sentinel-preserved.json"
fi
printf '%s\n' rollback-restored
