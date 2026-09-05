#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
cp -- "$PREIMAGE/AGENTS.md" "$TARGET/AGENTS.md"
for path in \
  "docs/apg/APG_BEGINNER_EXECUTOR_OFFLINE_CONTRACT.md" \
  "docs/apg/APG_GRILL_ME_UPSTREAM_ATTRIBUTION.md" \
  "scripts/apg_beginner_executor_preview.py" \
  "tests/test_apg_beginner_executor.py"; do
  rm -f -- "$TARGET/$path"
done
rm -rf -- "$TARGET/artifacts/apg-beginner-executor-offline"
printf '%s\n' "rollback-restored"
