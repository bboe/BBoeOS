#!/bin/sh

nasm -f bin -i src/include/ -i src/kernel/ -o os.bin src/kernel/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

for src in src/programs/*.asm; do
    name=$(basename "$src" .asm)
    nasm -f bin -i src/include/ -o "$tmpdir/$name" "$src"
    if [ $? -ne 0 ]; then
        exit 1
    fi
done

dd bs=512 count=2880 if=/dev/zero of=floppy.img
dd conv=notrunc if=os.bin of=floppy.img
for bin in "$tmpdir"/*; do
    ./add_file.sh -x floppy.img "$bin"
done
