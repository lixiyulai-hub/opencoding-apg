#!/usr/bin/env sh
set -eu
ROOT=${1:?project root path required}
for path in \
  "docs/apg/APG_AUTONOMOUS_PRG_POLICY.md" \
  "docs/apg/APG_AUTO_PLAN_LOOP_HARNESS.md" \
  "scripts/apg_autonomous_policy_preview.py" \
  "tests/test_apg_autonomous_policy.py"; do
  rm -f -- "$ROOT/$path"
done
rm -rf -- "$ROOT/artifacts/apg-autonomous-policy"
printf '%s\n' "rollback-restored"
