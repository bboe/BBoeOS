#!/bin/sh
# ports/kilo/fetch.sh — clones antirez/kilo (a ~1000-line single-file
# vi-like terminal editor, BSD-2) into third_party/ at a pinned commit
# so the build is reproducible.  Skips the clone if the destination
# already exists, so re-running is a no-op.
#
# kilo is the upstream editor we wrap with bboeos-specific adapters in
# ports/kilo/.  The source is not vendored in-tree (it would tie our git
# history to upstream's); we keep just this fetch script and the adapter
# shims.  Mirrors ports/doom/fetch.sh.

set -eu

cd "$(dirname "$0")/../.."

REPO_URL="https://github.com/antirez/kilo"
PINNED_COMMIT="323d93b29bd89a2cb446de90c4ed4fea1764176e"
DEST="third_party/kilo"

if [ -d "$DEST/.git" ]; then
    have=$(git -C "$DEST" rev-parse HEAD)
    if [ "$have" = "$PINNED_COMMIT" ]; then
        echo "kilo already at pinned commit ${PINNED_COMMIT}"
        exit 0
    fi
    echo "kilo at $have, want $PINNED_COMMIT — checking out"
    git -C "$DEST" fetch --quiet origin "$PINNED_COMMIT"
    git -C "$DEST" checkout --quiet "$PINNED_COMMIT"
    exit 0
fi

mkdir -p third_party
git clone --quiet "$REPO_URL" "$DEST"
git -C "$DEST" checkout --quiet "$PINNED_COMMIT"
echo "kilo cloned to $DEST at ${PINNED_COMMIT}"
