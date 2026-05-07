#!/usr/bin/env bash
# Fetch the OPL music subsystem from chocolate-doom upstream at a pinned
# commit into third_party/chocolate-doom-opl/.  Skips the download if a
# previous run wrote the same commit marker, so re-running is a no-op.
#
# Mirrors tools/fetch_doom.sh's idempotent shape — third_party/ is
# gitignored, so the fetched files never enter version control.  We
# don't apply local patches; chocolate_compat.h alone bridges
# chocolate-vs-doomgeneric drift via -include in tools/build_doom.py.
set -euo pipefail

COMMIT="35fb1372d10756ca27eca05665bd8a7cebc71c05"
BASE="https://raw.githubusercontent.com/chocolate-doom/chocolate-doom/${COMMIT}"
TARGET="$(cd "$(dirname "$0")/.." && pwd)/third_party/chocolate-doom-opl"
MARKER="${TARGET}/.fetched-commit"

if [ -f "${MARKER}" ] && [ "$(cat "${MARKER}")" = "${COMMIT}" ]; then
    echo "chocolate-doom OPL stack already at pinned commit ${COMMIT}"
    exit 0
fi

mkdir -p "${TARGET}"
for file in src/i_oplmusic.c src/midifile.c src/midifile.h \
            src/mus2mid.c src/mus2mid.h \
            src/memio.c src/memio.h \
            opl/opl.h opl/opl_queue.c opl/opl_queue.h; do
    curl -fsSL "${BASE}/${file}" -o "${TARGET}/$(basename "${file}")"
done
echo "${COMMIT}" > "${MARKER}"
echo "chocolate-doom OPL stack fetched to ${TARGET} at ${COMMIT}"
