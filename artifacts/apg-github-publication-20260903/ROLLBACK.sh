#!/usr/bin/env sh
set -eu
TARGET=${1:?disposable clone path required}
PREIMAGE=${2:?preimage commit required}
git -C "$TARGET" reset --hard "$PREIMAGE"
printf "%s\n" rollback-restored
