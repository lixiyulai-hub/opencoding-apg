#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
rm -f -- "$TARGET/.governance/progress/apg-adaptive-git-ledger.json" "$TARGET/.governance/progress/apg-adaptive-git-ledger.idempotency.json"
rm -f -- "$TARGET/docs/apg/APG_ADAPTIVE_GIT_LEDGER_PERSISTENCE_CONTRACT.md" "$TARGET/docs/apg/APG_ADAPTIVE_GIT_LEDGER_PERSISTENCE_PLAN.md" "$TARGET/scripts/apg_adaptive_git_ledger_persistence_preview.py" "$TARGET/tests/test_apg_adaptive_git_ledger_persistence.py"
rm -rf -- "$TARGET/artifacts/apg-adaptive-git-ledger-persistence-preview"
if [ -f "$PREIMAGE/scripts/apg_independent_review.py" ]; then cp -- "$PREIMAGE/scripts/apg_independent_review.py" "$TARGET/scripts/apg_independent_review.py"; fi
printf '%s\n' 'rollback-restored; ledger_removed=true; transaction_files_removed=true; preserved_receipts=true'
