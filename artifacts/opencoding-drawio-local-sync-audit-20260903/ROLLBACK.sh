#!/usr/bin/env bash
set -euo pipefail
target="${1:?target copy path required}"
preimage="${2:?preimage path required}"
cp -- "$preimage" "$target"
test -f "$target"
