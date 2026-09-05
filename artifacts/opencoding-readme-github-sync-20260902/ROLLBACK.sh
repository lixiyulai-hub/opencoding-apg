#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?disposable clone path}
COMMIT=${2:?commit SHA to revert}
git -C "$TARGET" revert --no-edit "$COMMIT"
printf 'disposable copy reverted %s\n' "$COMMIT"
