#!/usr/bin/env bash
set -euo pipefail
MODIFIED_FILE="${1:?modified file path required}"
BASELINE_FILE="${2:?baseline file path required}"
cp -- "$BASELINE_FILE" "$MODIFIED_FILE"
printf 'restored=%s\n' "$MODIFIED_FILE"
