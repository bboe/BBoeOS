#!/bin/sh

FS_TYPE=bbfs
IMAGE=drive.img
EXT2_BLOCK_SIZE=1024
EXT2_INODE_COUNT=
SECTORS=2880        # default = 1.44 MB floppy size; override for larger images
WITH_TEST_PROGRAMS=0
for arg in "$@"; do
    case "$arg" in
        --ext2) FS_TYPE=ext2 ;;
        --bbfs) FS_TYPE=bbfs ;;
        --ext2-block-size=*) EXT2_BLOCK_SIZE="${arg#*=}" ;;
        --ext2-inode-count=*) EXT2_INODE_COUNT="${arg#*=}" ;;
        --sectors=*) SECTORS="${arg#*=}" ;;
        --with-test-programs) WITH_TEST_PROGRAMS=1 ;;
        -*) echo "Unknown flag: $arg" >&2; exit 1 ;;
        *) IMAGE="$arg" ;;
    esac
done

KBUILD=build/kernel-c
rm -rf "$KBUILD" && mkdir -p "$KBUILD"
find src -name '*.c' -not -path 'src/c/*' | while read -r source; do
    rel="${source#src/}"; out="$KBUILD/${rel%.c}.kasm"
    mkdir -p "$(dirname "$out")"
    python3 cc.py --bits 32 --target kernel "$source" "$out" || exit 1
done

# Build the vDSO blob (FUNCTION_TABLE + shared_* helpers).  The kernel
# binary embeds it via incbin at the `vdso_image` label and copies it
# to virtual FUNCTION_TABLE (0x00010000) at boot via `vdso_install`.
nasm -f bin -i src/include/ -o vdso.bin src/vdso/vdso.asm
if [ $? -ne 0 ]; then
    exit 1
fi

# Two-pass build: assemble kernel.bin first, measure its sector count,
# then pass that as -DKERNEL_SECTORS=N when assembling boot.bin so the
# real-mode INT 13h knows how many sectors of kernel.bin to load into
# physical 0x10000.  boot.bin pads itself to a fixed BOOT_SECTORS so
# KERNEL_SECTORS is the only build-time variable; if boot.bin overflows
# the pad, NASM's `times` directive goes negative and the build fails.
nasm -f bin \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" -i "$KBUILD/arch/x86/" -i "$KBUILD/syscall/" \
    -o kernel.bin src/arch/x86/kernel.asm
if [ $? -ne 0 ]; then
    exit 1
fi

KERNEL_SIZE=$(wc -c < kernel.bin)
KERNEL_SECTORS=$(( ( KERNEL_SIZE + 511 ) / 512 ))

# Compute the first page above kernel.bin.  The kernel stack, NIC buffers,
# program-scratch buffer, boot PD, and first kernel PT are stacked here.
# KERNEL_LOAD_PHYS = 0x20000 (must match boot.asm + kernel.asm).
KERNEL_RESERVED_BASE=$(( (0x20000 + KERNEL_SIZE + 0xFFF) & ~0xFFF ))

# Safety: the entire kernel-side reserved region must stay below the
# VGA aperture at phys 0xA0000.  Worst-case region size: 4 KB stack +
# 4 KB boot PD + 4 KB first kernel PT + 128 KB frame_bitmap (sized at
# boot from E820, capped by FRAME_PHYSICAL_LIMIT ~ 4 GB, so the bitmap
# tops out at ~1M frames / 8 bits/byte = 128 KB on -m 4096) = 140 KB
# = 0x23000.  Keeping the whole region under 0xA0000 lets the OS boot
# under QEMU `-m 1` (1 MB total), where conventional RAM ends at
# 0x9FC00.  On `-m 1` the bitmap is actually ~20 bytes (one dword's
# worth), so the runtime region is much smaller than the worst case
# checked here.
# (sector_buffer / ext2_sd_buffer share a dynamically-allocated 4 KB
# fs scratch frame populated by `vfs_init`, so they no longer occupy
# a static slot in this region.)
KERNEL_RESERVED_END=$(( KERNEL_RESERVED_BASE + 0x23000 ))
if [ $KERNEL_RESERVED_END -ge $(( 0xA0000 )) ]; then
    echo "make_os.sh: kernel reserved region (ends at $(printf 0x%x $KERNEL_RESERVED_END)) crosses the VGA hole at 0xA0000" >&2
    exit 1
fi

# Second pass: re-assemble kernel.bin with the correct layout anchor.
# All immediates are 32-bit regardless of value, so the size is invariant.
nasm -f bin \
    -DKERNEL_RESERVED_BASE=$KERNEL_RESERVED_BASE \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" -i "$KBUILD/arch/x86/" -i "$KBUILD/syscall/" \
    -o kernel.bin src/arch/x86/kernel.asm
if [ $? -ne 0 ]; then
    exit 1
fi

KERNEL_SIZE_2=$(wc -c < kernel.bin)
if [ "$KERNEL_SIZE_2" -ne "$KERNEL_SIZE" ]; then
    echo "make_os.sh: kernel.bin size changed between passes ($KERNEL_SIZE → $KERNEL_SIZE_2)" >&2
    exit 1
fi

nasm -f bin \
    -DKERNEL_SECTORS=$KERNEL_SECTORS \
    -DKERNEL_RESERVED_BASE=$KERNEL_RESERVED_BASE \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" \
    -o boot.bin src/arch/x86/boot/boot.asm
if [ $? -ne 0 ]; then
    exit 1
fi

cat boot.bin kernel.bin > os.bin

# User-facing programs are every src/c/*.c file; test-only fixtures
# live under tests/programs/ and only ship in the drive image when
# --with-test-programs is passed (default builds keep them out so a
# normal boot has just the user programs).  Both lists are sorted so
# the on-disk directory layout is stable across builds.
USER_PROGRAMS=$(find src/c -maxdepth 1 -name '*.c' | sed 's|.*/||; s/\.c$//' | sort | tr '\n' ' ')
TEST_PROGRAMS=$(find tests/programs -maxdepth 1 -name '*.c' | sed 's|.*/||; s/\.c$//' | sort | tr '\n' ' ')
if [ "$WITH_TEST_PROGRAMS" -eq 1 ]; then
    PROGRAMS="$USER_PROGRAMS $TEST_PROGRAMS"
else
    PROGRAMS="$USER_PROGRAMS"
fi

PBUILD=build/c
rm -rf "$PBUILD" && mkdir -p "$PBUILD"
for name in $USER_PROGRAMS; do
    python3 cc.py --bits 32 "src/c/$name.c" "$PBUILD/$name.asm" || exit 1
    nasm -f bin -i src/include/ -o "$PBUILD/$name" "$PBUILD/$name.asm" || exit 1
done
if [ "$WITH_TEST_PROGRAMS" -eq 1 ]; then
    for name in $TEST_PROGRAMS; do
        python3 cc.py --bits 32 "tests/programs/$name.c" "$PBUILD/$name.asm" || exit 1
        nasm -f bin -i src/include/ -o "$PBUILD/$name" "$PBUILD/$name.asm" || exit 1
    done
fi

dd bs=512 count="$SECTORS" if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"

if [ "$FS_TYPE" = "ext2" ]; then
    EXT2_START=$(python3 -c "from add_file import compute_directory_sector; print(compute_directory_sector(image_path='$IMAGE'))") || exit 1
    mke2fs -b "$EXT2_BLOCK_SIZE" -t ext2 -m 0 -E offset=$((EXT2_START * 512)) ${EXT2_INODE_COUNT:+-N "$EXT2_INODE_COUNT"} "$IMAGE" $(( (SECTORS - EXT2_START) / 2 )) || exit 1
fi

./add_file.py --mkdir --image "$IMAGE" bin || exit 1
PROGRAM_PATHS=""
for name in $PROGRAMS; do
    PROGRAM_PATHS="$PROGRAM_PATHS $PBUILD/$name"
done
./add_file.py -x -d bin --image "$IMAGE" $PROGRAM_PATHS || exit 1

# Static reference files used by cat / cp / asm tests.
./add_file.py --mkdir --image "$IMAGE" src || exit 1
STATIC_FILES=""
for f in static/*; do
    [ -f "$f" ] && STATIC_FILES="$STATIC_FILES $f"
done
if [ -n "$STATIC_FILES" ]; then
    ./add_file.py -d src --image "$IMAGE" $STATIC_FILES || exit 1
fi
