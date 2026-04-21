#!/bin/sh
# tests/test_idt.sh — build and run the standalone IDT smoke test.
#
# Expected output:  R  then  P  then  EXC00  on the serial console,
# confirming that the IDT caught a divide-by-zero after the pmode switch.

set -e

cd "$(dirname "$0")/.."

nasm -f bin -i src/kernel/ -o tests/idt_test.bin tests/idt_test.asm

IMAGE=tests/idt_test.img
dd bs=512 count=2880 if=/dev/zero of="$IMAGE" status=none
dd conv=notrunc if=tests/idt_test.bin of="$IMAGE" status=none

echo "Booting IDT smoke test (expecting 'R', 'P', 'EXC00')..."
timeout 5 qemu-system-i386 \
        -drive file="$IMAGE",format=raw \
        -display none \
        -serial stdio \
        -monitor none \
        -no-reboot || true
echo "(qemu exited)"
