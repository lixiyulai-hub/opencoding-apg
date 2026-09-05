#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
for path in \
  "docs/apg/APG_ADAPTIVE_GIT_CONTROLLER_CONTRACT.md" \
  "scripts/apg_adaptive_git_controller.py" \
  "tests/test_apg_adaptive_git_controller.py"; do
  rm -f -- "$TARGET/$path"
done
rm -rf -- "$TARGET/artifacts/apg-adaptive-git-controller-connection"
if [ -f "$PREIMAGE/scripts/apg_independent_review.py" ]; then
  mkdir -p -- "$TARGET/scripts"
  cp -- "$PREIMAGE/scripts/apg_independent_review.py" "$TARGET/scripts/apg_independent_review.py"
fi
printf '%s\n' "rollback-restored"
