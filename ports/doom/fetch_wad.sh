#!/bin/sh
# tools/fetch_wad.sh — download the Doom 1.9 shareware WAD into wads/.
#
# id Software released the Doom shareware episode as freely
# redistributable (DOOM Software Limited Distribution License,
# https://web.archive.org/web/20180924111811/http://www.gamers.org/dEngine/doom/papers/license.html).
# We fetch doom1.wad from a stable mirror and verify the SHA256 so the
# build is reproducible and we don't ship a corrupted blob.
#
# Re-running with the file already in place is a no-op.  The wads/
# directory is gitignored so the WAD never enters our git history.

set -eu

DEST="wads/doom1.wad"
URL="https://distro.ibiblio.org/slitaz/sources/packages/d/doom1.wad"
EXPECTED_SHA256="1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"

verify() {
    actual=$(sha256sum "$1" | awk '{print $1}')
    if [ "$actual" != "$EXPECTED_SHA256" ]; then
        echo "fetch_wad: SHA256 mismatch on $1" >&2
        echo "  expected: $EXPECTED_SHA256" >&2
        echo "  actual:   $actual" >&2
        return 1
    fi
}

if [ -f "$DEST" ] && verify "$DEST" 2>/dev/null; then
    echo "doom1.wad already present and verified at $DEST"
    exit 0
fi

mkdir -p wads
echo "fetching doom1.wad from $URL"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$DEST" "$URL"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$DEST" "$URL"
else
    echo "fetch_wad: neither curl nor wget is available" >&2
    exit 1
fi

if ! verify "$DEST"; then
    rm -f "$DEST"
    echo "fetch_wad: download verification failed; removed $DEST" >&2
    echo "  if your network rewrites HTTPS responses, supply doom1.wad" >&2
    echo "  manually at $DEST and re-run." >&2
    exit 1
fi
echo "doom1.wad fetched to $DEST (SHA256 verified)"
