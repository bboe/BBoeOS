#!/bin/sh
# Re-measure each kernel-port row's ASM/C/Δ against the current main.
#
# For each driver listed below: swap its %include from `<name>.kasm` →
# `<name>.asm`, remove `src/<rel>/<name>.c` so make_os.sh skips its
# compilation, build, record os.bin size as ASM-bytes; restore the .c
# file and the .kasm include, build, record size as C-bytes; print the
# delta.  The C-bytes value is always the current full-C build (one
# build at start), so each row's C column is the same.
#
# Run from the worktree root.  Leaves the tree clean on exit even if a
# build fails partway through.
set -e

# Each entry is "<rel-from-src>:<include-pattern>:<asm-host-file>".  The
# include pattern is what appears between %include "..." in the host
# asm file — usually the relative path under src/<root> minus the
# .asm/.kasm extension, but `arch/x86/system` just appears as `system`
# because that include resolves via nasm's -i src/arch/x86 path.  The
# host file is the .asm whose %include line gets sed'd between .asm
# and .kasm; defaults to src/arch/x86/kernel.asm but
# fs/fd/<name> ports route through src/fs/fd.c (which is itself an
# asm() block).
ROW_LIST="
arch/x86/system:system:src/arch/x86/kernel.asm
drivers/ata:drivers/ata:src/arch/x86/kernel.asm
drivers/console:drivers/console:src/arch/x86/kernel.asm
drivers/fdc:drivers/fdc:src/arch/x86/kernel.asm
drivers/ne2k:drivers/ne2k:src/arch/x86/kernel.asm
drivers/ps2:drivers/ps2:src/arch/x86/kernel.asm
drivers/rtc:drivers/rtc:src/arch/x86/kernel.asm
drivers/serial:drivers/serial:src/arch/x86/kernel.asm
drivers/vga:drivers/vga:src/arch/x86/kernel.asm
fs/fd/console:fs/fd/console:src/fs/fd.c
fs/fd/fs:fs/fd/fs:src/fs/fd.c
fs/fd/net:fs/fd/net:src/fs/fd.c
"

cleanup() {
    for entry in $ROW_LIST; do
        host=${entry##*:}
        host_bak="/tmp/measure_host_$(echo "$host" | tr / _).bak"
        if [ -f "$host_bak" ]; then
            mv "$host_bak" "$host"
        fi
    done
    for entry in $ROW_LIST; do
        row=${entry%%:*}
        rm -f "src/${row}.asm"
        bak="/tmp/measure_$(echo "$row" | tr / _).c"
        if [ -f "$bak" ]; then
            mv "$bak" "src/${row}.c"
        fi
    done
}
trap cleanup EXIT

echo "Building current full-C kernel..."
./make_os.sh > /dev/null 2>&1
C_BYTES=$(stat -c %s os.bin)
echo "  C (full kernel) = $C_BYTES bytes"
echo

printf "| File | ASM (bytes) | C (bytes) | Delta |\n"
printf "|------|-------------|-----------|-------|\n"

for entry in $ROW_LIST; do
    row=${entry%%:*}
    rest=${entry#*:}
    inc=${rest%:*}
    host=${rest#*:}
    bak="/tmp/measure_$(echo "$row" | tr / _).c"

    host_bak="/tmp/measure_host_$(echo "$host" | tr / _).bak"
    cp "$host" "$host_bak"
    cp "archive/kernel/${row}.asm" "src/${row}.asm"
    mv "src/${row}.c" "$bak"
    sed -i "s|${inc}.kasm|${inc}.asm|" "$host"

    if ! ./make_os.sh > /tmp/build.log 2>&1; then
        echo "BUILD FAILED for $row" >&2
        tail -20 /tmp/build.log >&2
        exit 1
    fi
    asm_bytes=$(stat -c %s os.bin)
    delta=$((C_BYTES - asm_bytes))

    printf "| %s | %d | %d | %+d |\n" "$row" "$asm_bytes" "$C_BYTES" "$delta"

    # Restore for next iteration.
    rm "src/${row}.asm"
    mv "$bak" "src/${row}.c"
    mv "$host_bak" "$host"
done
