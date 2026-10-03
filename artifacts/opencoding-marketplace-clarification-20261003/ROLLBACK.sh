#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT="${1:?implementation commit required}"
git revert --no-edit "$CHECKPOINT"
