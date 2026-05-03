#!/bin/sh
# tools/fetch_doom.sh — clones doomgeneric (GPLv2) into third_party/
# at a pinned commit so the build is reproducible.  Skips the clone if
# the destination already exists, so re-running is a no-op.
#
# doomgeneric is the upstream Doom port we wrap with bboeos-specific
# adapters in tools/doom/.  The full source is too large to vendor
# in-tree and would tie our git history to upstream's; we keep just
# this fetch script and the adapter shims.

set -eu

REPO_URL="https://github.com/ozkl/doomgeneric"
PINNED_COMMIT="dcb7a8dbc7a16ce3dda29382ac9aae9d77d21284"
DEST="third_party/doomgeneric"

if [ -d "$DEST/.git" ]; then
    have=$(git -C "$DEST" rev-parse HEAD)
    if [ "$have" = "$PINNED_COMMIT" ]; then
        echo "doomgeneric already at pinned commit ${PINNED_COMMIT}"
        exit 0
    fi
    echo "doomgeneric at $have, want $PINNED_COMMIT — checking out"
    git -C "$DEST" fetch --quiet origin "$PINNED_COMMIT"
    git -C "$DEST" checkout --quiet "$PINNED_COMMIT"
    exit 0
fi

mkdir -p third_party
git clone --quiet "$REPO_URL" "$DEST"
git -C "$DEST" checkout --quiet "$PINNED_COMMIT"
echo "doomgeneric cloned to $DEST at ${PINNED_COMMIT}"
