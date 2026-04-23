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

nasm -f bin -i src/include/ -i src/arch/x86/ -i src/arch/x86/boot/ -o os.bin src/arch/x86/boot/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

for src in src/c/*.c; do
    [ -f "$src" ] || continue
    name=$(basename "$src" .c)
    python3 cc.py "$src" "$tmpdir/$name.asm"
    if [ $? -ne 0 ]; then
        exit 1
    fi
    nasm -f bin -i src/include/ -o "$tmpdir/$name" "$tmpdir/$name.asm"
    if [ $? -ne 0 ]; then
        exit 1
    fi
    rm "$tmpdir/$name.asm"
done

dd bs=512 count=2880 if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"

if [ "$FS_TYPE" = "ext2" ]; then
    EXT2_START=$(python3 -c "from add_file import read_assign; print(read_assign('EXT2_START_SECTOR'))")
    mke2fs -b "$EXT2_BLOCK_SIZE" -t ext2 -m 0 -E offset=$((EXT2_START * 512)) "$IMAGE" $(( (2880 - EXT2_START) / 2 ))
    if [ $? -ne 0 ]; then
        exit 1
    fi
fi

./add_file.py --image "$IMAGE" --mkdir bin
for bin in "$tmpdir"/*; do
    ./add_file.py --image "$IMAGE" -x -d bin "$bin"
done

# Add static files (non-executable) into src/
./add_file.py --image "$IMAGE" --mkdir src
for f in static/*; do
    [ -f "$f" ] && ./add_file.py --image "$IMAGE" -d src "$f"
done
