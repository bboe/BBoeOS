#!/bin/sh
# tests/test_pmode.sh — build and run the standalone protected mode smoke test.
#
# Expected output: two lines on the serial console, "R" then "P", confirming
# the 16-bit entry ran and the 32-bit code path took over after the switch.

set -e

cd "$(dirname "$0")/.."

nasm -f bin -i src/arch/x86/ -i src/arch/x86/boot/ -o tests/pmode_test.bin tests/pmode_test.asm

IMAGE=tests/pmode_test.img
dd bs=512 count=2880 if=/dev/zero of="$IMAGE" status=none
dd conv=notrunc if=tests/pmode_test.bin of="$IMAGE" status=none

echo "Booting protected mode smoke test (expecting 'R' then 'P' on serial)..."
timeout 5 qemu-system-i386 \
        -drive file="$IMAGE",format=raw \
        -display none \
        -serial stdio \
        -monitor none \
        -no-reboot || true
echo "(qemu exited)"
