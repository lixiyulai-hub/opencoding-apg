#!/usr/bin/env sh
set -eu
TARGET=${1:?target root path required}
PREIMAGE=${2:?preimage root path required}
for path in \
  "docs/apg/APG_GITHUB_PUBLIC_DELIVERY_PREVIEW_20260902.md" \
  "docs/apg/APG_HOMEPAGE_HERO_PREVIEW_CN.md" \
  "docs/apg/APG_DRAWIO_GITHUB_INTRO_PREVIEW.md"; do
  rm -f -- "$TARGET/$path"
done
rm -rf -- "$TARGET/artifacts/apg-github-public-delivery-preview"
printf '%s\n' "rollback-restored"
