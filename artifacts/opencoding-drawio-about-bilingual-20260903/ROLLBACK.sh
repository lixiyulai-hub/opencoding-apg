#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?disposable clone path}
COMMIT=${2:?diagram commit SHA}
git -C "$TARGET" revert --no-edit "$COMMIT"
