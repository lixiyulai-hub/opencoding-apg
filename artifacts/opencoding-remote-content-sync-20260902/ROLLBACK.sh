#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?target clone path}
COMMIT=${2:?sync commit SHA}
git -C "$TARGET" revert --no-edit "$COMMIT"
