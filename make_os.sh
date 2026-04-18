#!/bin/sh

nasm -f bin -i src/include/ -i src/kernel/ -o os.bin src/kernel/bboeos.asm
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

IMAGE="${1:-drive.img}"
dd bs=512 count=2880 if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"
./add_file.py --image "$IMAGE" --mkdir bin
for bin in "$tmpdir"/*; do
    ./add_file.py --image "$IMAGE" -x -d bin "$bin"
done

# Add static files (non-executable) into src/
./add_file.py --image "$IMAGE" --mkdir src
for f in static/*; do
    [ -f "$f" ] && ./add_file.py --image "$IMAGE" -d src "$f"
done
