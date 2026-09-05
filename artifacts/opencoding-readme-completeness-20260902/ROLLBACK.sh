#!/usr/bin/env bash
set -euo pipefail
TARGET=${1:?target copy path}
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
mkdir -p "$TARGET"
cp "$SCRIPT_DIR/README.md.before" "$TARGET/README.md"
cp "$SCRIPT_DIR/README_CN.md.before" "$TARGET/README_CN.md"
printf 'restored README.md and README_CN.md from baseline preimages\n'
