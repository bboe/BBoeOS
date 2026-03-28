#!/bin/sh

nasm -f bin -i src/kernel/ -o os.bin src/kernel/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

dd bs=512 count=2880 if=/dev/zero of=floppy.img
dd conv=notrunc if=os.bin of=floppy.img
#rm bboeos.bin
