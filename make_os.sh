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

nasm -f bin \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" \
    -o os.bin src/arch/x86/boot/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

# Curated list of pmode-ready user programs.  A program belongs here once
# every FUNCTION_TABLE slot it calls (see src/include/constants.asm) is
# routed to a real ported helper rather than `shared_not_impl` — otherwise
# invoking it halts the kernel.  Programs that need PARSE_ARGV /
# PRINT_DATETIME / PRINT_IP / PRINT_MAC / PRINT_BYTE_DECIMAL / PRINT_HEX /
# PRINT_DECIMAL stay out of this list until those helpers are ported.
PROGRAMS="asmesc bits booltest cftest draw fctest gdemo gtable hello inctest loop pintest shell uptime"

PBUILD=build/c
rm -rf "$PBUILD" && mkdir -p "$PBUILD"
for name in $PROGRAMS; do
    python3 cc.py --bits 32 "src/c/$name.c" "$PBUILD/$name.asm" || exit 1
    nasm -f bin -i src/include/ -o "$PBUILD/$name" "$PBUILD/$name.asm" || exit 1
done

dd bs=512 count=2880 if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"
./add_file.py --mkdir --image "$IMAGE" bin || exit 1
for name in $PROGRAMS; do
    ./add_file.py -x -d bin --image "$IMAGE" "$PBUILD/$name" || exit 1
done
