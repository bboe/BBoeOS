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

python3 cc.py --bits 32 src/c/hello.c build/hello.asm || exit 1
nasm -f bin -i src/include/ -o hello build/hello.asm || exit 1

python3 cc.py --bits 32 src/c/shell.c build/shell.asm || exit 1
nasm -f bin -i src/include/ -o shell build/shell.asm || exit 1

dd bs=512 count=2880 if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"
./add_file.py --mkdir --image "$IMAGE" bin || exit 1
./add_file.py -x --image "$IMAGE" hello || exit 1
./add_file.py -x -d bin --image "$IMAGE" shell || exit 1
