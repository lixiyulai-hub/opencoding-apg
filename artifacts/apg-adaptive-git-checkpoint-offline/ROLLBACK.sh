#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
for path in "docs/apg/APG_ADAPTIVE_GIT_CHECKPOINT_CONTRACT.md" "docs/apg/APG_GIT_CHECKPOINT_POLICY.md" "scripts/apg_adaptive_git_preview.py" "tests/test_apg_adaptive_git.py"; do rm -f -- "$TARGET/$path"; done
rm -rf -- "$TARGET/artifacts/apg-adaptive-git-checkpoint-offline"
printf '%s\n' "rollback-restored"
