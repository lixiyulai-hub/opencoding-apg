#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?project copy path}
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/PREIMAGE_README.md" "$TARGET/README.md"
rm -f "$TARGET/README_CN.md"
