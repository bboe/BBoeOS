#!/bin/sh

nasm -f bin -o bboeos.bin bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

dd bs=512 count=2880 if=/dev/zero of=floppy.img
dd status=noxfer conv=notrunc if=bboeos.bin of=floppy.img
#rm bboeos.bin
