#!/bin/sh

nasm -f bin -i src/include/ -i src/kernel/ -o os.bin src/kernel/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

for src in src/asm/*.asm; do
    name=$(basename "$src" .asm)
    nasm -f bin -i src/include/ -o "$tmpdir/$name" "$src"
    if [ $? -ne 0 ]; then
        exit 1
    fi
done

dd bs=512 count=2880 if=/dev/zero of=drive.img
dd conv=notrunc if=os.bin of=drive.img
./add_file.py --mkdir bin
for bin in "$tmpdir"/*; do
    ./add_file.py -x -d bin "$bin"
done

# Add static files (non-executable) into src/
./add_file.py --mkdir src
for f in static/*; do
    [ -f "$f" ] && ./add_file.py -d src "$f"
done
