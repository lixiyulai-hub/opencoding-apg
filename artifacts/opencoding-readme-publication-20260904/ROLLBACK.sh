#!/usr/bin/env sh
set -eu
clone="${1:?disposable clone path required}"
pre_publish="${2:?pre-publish commit required}"
git -C "$clone" revert --no-edit HEAD
test "$(git -C "$clone" rev-parse HEAD^ )" = "$pre_publish"
echo "rollback rehearsal complete: $pre_publish"
