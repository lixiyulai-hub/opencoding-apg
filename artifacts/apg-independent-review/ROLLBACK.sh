#!/usr/bin/env sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: ROLLBACK.sh <pre-state> <target>" >&2
  exit 2
fi
cp -- "$1" "$2"
printf '%s\n' "rollback-restored"
