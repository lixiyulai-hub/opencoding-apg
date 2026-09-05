#!/usr/bin/env sh
set -eu
TARGET_ROOT="${1:?target root required}"
PREIMAGE_ROOT="${2:?preimage root required}"
REL="artifacts/opencoding-brand-rename-preview-20260902/MODIFIED_FILE"
TARGET="${TARGET_ROOT}/${REL}"
PREIMAGE="${PREIMAGE_ROOT}/PROJECT_BRIEF.md"
test -f "${TARGET}"
test -f "${PREIMAGE}"
cp "${PREIMAGE}" "${TARGET}"
cmp -s "${PREIMAGE}" "${TARGET}"
printf "%s\n" ROLLBACK_RESTORED
