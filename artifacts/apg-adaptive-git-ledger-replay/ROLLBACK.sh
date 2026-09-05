#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
for path in \
  "docs/apg/APG_ADAPTIVE_GIT_LEDGER_CONTRACT.md" \
  "scripts/apg_adaptive_git_ledger_preview.py" \
  "tests/test_apg_adaptive_git_ledger.py"; do
  rm -f -- "$TARGET/$path"
done
rm -rf -- "$TARGET/artifacts/apg-adaptive-git-ledger-replay"
for path in \
  "scripts/apg_adaptive_git_controller.py" \
  "scripts/apg_independent_review.py" \
  "tests/test_apg_adaptive_git_controller.py" \
  "tests/test_apg_independent_review.py"; do
  if [ -f "$PREIMAGE/$path" ]; then
    mkdir -p -- "$(dirname "$TARGET/$path")"
    cp -- "$PREIMAGE/$path" "$TARGET/$path"
  fi
done
printf '%s\n' "rollback-restored"
