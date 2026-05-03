#!/bin/sh
# tools/install_doom.sh — build + install Doom on a fresh bboeos disk image.
#
# Convenience wrapper around the four steps you'd otherwise run manually:
#   1. python3 tools/build_doom.py        (compile bin/doom into build/doom/)
#   2. ./make_os.sh --ext2 --sectors=...  (10 MB ext2 image)
#   3. ./add_file.py … bin/doom           (drop the binary in /bin)
#   4. ./add_file.py … doom1.wad          (drop the WAD at the disk root)
#
# Idempotent: re-running rebuilds bin/doom (cached at the .o level) and
# regenerates drive.img from scratch.  Prints the qemu command to run
# the result.
#
# Flags:
#   --image PATH       drive image to write (default: drive.img)
#   --sectors N        image size in 512-byte sectors (default: 20480 = 10 MB)
#   --wad PATH         WAD to install (default: wads/doom1.wad)

set -eu

IMAGE=drive.img
SECTORS=20480
WAD=wads/doom1.wad
for arg in "$@"; do
    case "$arg" in
        --image=*)   IMAGE="${arg#*=}" ;;
        --sectors=*) SECTORS="${arg#*=}" ;;
        --wad=*)     WAD="${arg#*=}" ;;
        *) echo "unknown flag: $arg" >&2; exit 1 ;;
    esac
done

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

if [ ! -f "$WAD" ]; then
    echo "install_doom: $WAD missing — run tools/fetch_wad.sh first" >&2
    exit 1
fi

echo "==> building bin/doom (clean rebuild)"
# --clean forces a full rebuild of doomgeneric + our backend; we also
# wipe the libc .o cache because the libc Makefile doesn't track CFLAGS
# changes (so a flag-only flip would otherwise leave libbboeos.a stale).
make -C tools/libc clean
python3 tools/build_doom.py --clean

echo "==> building $IMAGE (ext2, $SECTORS sectors)"
./make_os.sh --ext2 --sectors="$SECTORS" "$IMAGE"

echo "==> installing bin/doom + $(basename "$WAD")"
./add_file.py -x -d bin --image "$IMAGE" build/doom/doom
./add_file.py --image "$IMAGE" "$WAD"

cat <<EOF

doom installed on $IMAGE.  Boot with:

  qemu-system-i386 -m 64 -drive file=$IMAGE,format=raw -serial stdio

then at the bboeos shell prompt:

  \$ doom
EOF
