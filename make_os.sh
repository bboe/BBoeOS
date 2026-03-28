#!/bin/sh

nasm -f bin -i src/include/ -i src/kernel/ -o os.bin src/kernel/bboeos.asm
if [ $? -ne 0 ]; then
    exit 1
fi

nasm -f bin -i src/include/ -o date src/programs/date.asm
if [ $? -ne 0 ]; then
    exit 1
fi

nasm -f bin -i src/include/ -o shell src/programs/shell.asm
if [ $? -ne 0 ]; then
    exit 1
fi

nasm -f bin -i src/include/ -o uptime src/programs/uptime.asm
if [ $? -ne 0 ]; then
    exit 1
fi

dd bs=512 count=2880 if=/dev/zero of=floppy.img
dd conv=notrunc if=os.bin of=floppy.img
./add_file.sh floppy.img date
./add_file.sh floppy.img shell
./add_file.sh floppy.img uptime
rm -f date shell uptime
