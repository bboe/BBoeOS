#!/bin/sh
# measure_kernel_port.sh <path>
#
# Print os.bin byte sizes for both the C-ported and archived-asm forms
# of one kernel file, plus their delta.  Used after a kernel port to
# fill in the archive/kernel/README.md row.
#
# Usage:
#     tests/measure_kernel_port.sh drivers/ps2
#
# Prerequisites (run AFTER the port is staged):
#     src/<path>.c                — the C port (cc.py compiles it)
#     archive/kernel/<path>.asm   — the snapshot of the original
#     bboeos.asm                  — already references .kasm for the
#                                   ported file (port commits do this)
#
# Mechanism:
#     1. ./make_os.sh produces build/kernel-c/<path>.kasm from the C
#        port; record os.bin size as the C size.
#     2. Overwrite build/kernel-c/<path>.kasm with a one-line shim
#        '%include "<path>.asm"' and add archive/kernel/ to NASM's
#        include path so the include resolves against the snapshot.
#     3. Re-run only the NASM stage of the build (skipping cc.py)
#        and record os.bin size as the ASM size.
#
# Cleanup is not required: any subsequent ./make_os.sh wipes
# build/kernel-c/ before recompiling.

set -e

if [ $# -ne 1 ]; then
    echo "Usage: $0 <path>" >&2
    echo "Example: $0 drivers/ps2" >&2
    exit 1
fi

PATH_NAME="$1"
SRC_C="src/${PATH_NAME}.c"
ARCHIVE_ASM="archive/kernel/${PATH_NAME}.asm"
KASM_PATH="build/kernel-c/${PATH_NAME}.kasm"

if [ ! -f "$SRC_C" ]; then
    echo "Missing $SRC_C — port hasn't been written yet." >&2
    exit 1
fi
if [ ! -f "$ARCHIVE_ASM" ]; then
    echo "Missing $ARCHIVE_ASM — snapshot the original first." >&2
    exit 1
fi

# 1. Build with the C port active.
./make_os.sh >/dev/null 2>&1
C_SIZE=$(stat -c %s os.bin)

# 2. Replace the cc.py-generated .kasm with a shim that pulls in the
#    archived hand-written asm, then re-run only the NASM stage.
echo "%include \"${PATH_NAME}.asm\"" > "$KASM_PATH"

nasm -f bin \
    -i src/include/ -i src/ -i src/arch/x86/ -i src/arch/x86/boot/ \
    -i build/kernel-c/ -i build/kernel-c/net/ -i build/kernel-c/fs/ \
    -i build/kernel-c/arch/x86/ \
    -i archive/kernel/ \
    -o os.bin src/arch/x86/boot/bboeos.asm

ASM_SIZE=$(stat -c %s os.bin)

# 3. Report.
DELTA=$((C_SIZE - ASM_SIZE))
if [ "$DELTA" -ge 0 ]; then
    DELTA_FMT="+${DELTA}"
else
    DELTA_FMT="${DELTA}"
fi

printf 'ASM:   %5d bytes  (archive/kernel/%s.asm)\n' "$ASM_SIZE" "$PATH_NAME"
printf 'C:     %5d bytes  (src/%s.c)\n' "$C_SIZE" "$PATH_NAME"
printf 'Delta: %s bytes\n' "$DELTA_FMT"

# Re-run a normal build at the end so the working tree's os.bin
# matches the committed source state, not the asm-shim variant.
./make_os.sh >/dev/null 2>&1
