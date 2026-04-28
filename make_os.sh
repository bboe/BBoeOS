#!/bin/sh

FS_TYPE=bbfs
IMAGE=drive.img
EXT2_BLOCK_SIZE=1024
for arg in "$@"; do
    case "$arg" in
        --ext2) FS_TYPE=ext2 ;;
        --bbfs) FS_TYPE=bbfs ;;
        --ext2-block-size=*) EXT2_BLOCK_SIZE="${arg#*=}" ;;
        -*) echo "Unknown flag: $arg" >&2; exit 1 ;;
        *) IMAGE="$arg" ;;
    esac
done

KBUILD=build/kernel-c
rm -rf "$KBUILD" && mkdir -p "$KBUILD"
find src -name '*.c' -not -path 'src/c/*' | while read -r source; do
    rel="${source#src/}"; out="$KBUILD/${rel%.c}.kasm"
    mkdir -p "$(dirname "$out")"
    python3 cc.py --target kernel "$source" "$out" || exit 1
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
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" \
    -o kernel.bin src/arch/x86/kernel.asm
if [ $? -ne 0 ]; then
    exit 1
fi

KERNEL_SECTORS=$(( ( $(wc -c < kernel.bin) + 511 ) / 512 ))

nasm -f bin \
    -DKERNEL_SECTORS=$KERNEL_SECTORS \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" \
    -o boot.bin src/arch/x86/boot/boot.asm
if [ $? -ne 0 ]; then
    exit 1
fi

cat boot.bin kernel.bin > os.bin

# Curated list of protected mode-ready user programs.  arp / dns / netinit /
# netrecv / netsend / ping stay out of the list until ne2k.asm and
# net/*.asm get ported to 32-bit and `network_initialize` runs from
# `protected_mode_entry`.
PROGRAMS="arp asm asmesc bits booltest cat cftest chmod cp date dns draw echo edit fctest gdemo gtable hello inctest loop loop_array ls mkdir mv netinit netrecv netsend pintest ping rm rmdir shell uptime"

PBUILD=build/c
rm -rf "$PBUILD" && mkdir -p "$PBUILD"
for name in $PROGRAMS; do
    python3 cc.py --bits 32 "src/c/$name.c" "$PBUILD/$name.asm" || exit 1
    nasm -f bin -i src/include/ -o "$PBUILD/$name" "$PBUILD/$name.asm" || exit 1
done

dd bs=512 count=2880 if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"

if [ "$FS_TYPE" = "ext2" ]; then
    EXT2_START=$(python3 -c "from add_file import compute_directory_sector; print(compute_directory_sector(image_path='$IMAGE'))") || exit 1
    mke2fs -b "$EXT2_BLOCK_SIZE" -t ext2 -m 0 -E offset=$((EXT2_START * 512)) "$IMAGE" $(( (2880 - EXT2_START) / 2 )) || exit 1
fi

./add_file.py --mkdir --image "$IMAGE" bin || exit 1
for name in $PROGRAMS; do
    ./add_file.py -x -d bin --image "$IMAGE" "$PBUILD/$name" || exit 1
done

# Static reference files used by cat / cp / asm tests.
./add_file.py --mkdir --image "$IMAGE" src || exit 1
for f in static/*; do
    [ -f "$f" ] && ./add_file.py -d src --image "$IMAGE" "$f" || exit 1
done
