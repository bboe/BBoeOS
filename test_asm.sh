#!/bin/sh
#
# Test that the self-hosted assembler produces byte-identical output
# to NASM for all programs in static/.
#
# Usage: ./test_asm.sh [program-name]
#   With no argument, tests every program in static/.
#   With a name (e.g. ./test_asm.sh edit), tests only that one program.
#   The artifacts (/tmp/ref_<name>.bin, /tmp/out_<name>.bin) are kept on
#   single-program runs so the output can be inspected after a failure.
#
# Requires: nasm, qemu-system-i386

set -e

PASS=0
FAIL=0
ERRORS=""
ONLY="${1:-}"

# Build the OS
echo "Building OS..."
./make_os.sh > /dev/null 2>&1

# Find programs in static/ with "org 6000h"; honour single-program filter.
PROGRAMS=""
for f in static/*.asm; do
    grep -q "org 6000h" "$f" 2>/dev/null || continue
    if [ -n "$ONLY" ]; then
        case "$(basename "$f" .asm)" in
            "$ONLY") PROGRAMS="$PROGRAMS $f" ;;
        esac
    else
        PROGRAMS="$PROGRAMS $f"
    fi
done

if [ -n "$ONLY" ] && [ -z "$PROGRAMS" ]; then
    echo "No program named '$ONLY' in static/"
    exit 1
fi

if [ -z "$PROGRAMS" ]; then
    echo "No programs found in static/"
    exit 1
fi

echo "Programs to test: $(echo $PROGRAMS | tr ' ' '\n' | xargs -n1 basename | tr '\n' ' ')"
echo ""

# Generate NASM reference binaries
for src in $PROGRAMS; do
    name=$(basename "$src" .asm)
    nasm -f bin -o "/tmp/ref_${name}.bin" "$src" -I static/
done

# Test each program in a fresh QEMU session (avoids directory-full issues)
for src in $PROGRAMS; do
    name=$(basename "$src" .asm)
    out="${name}_t"
    ref="/tmp/ref_${name}.bin"
    ref_size=$(wc -c < "$ref")

    # Fresh floppy for each test
    cp drive.img "/tmp/test_floppy_${name}.img"

    rm -f /tmp/asm_ser.in /tmp/asm_ser.out /tmp/asm_mon.in /tmp/asm_mon.out
    mkfifo /tmp/asm_ser.in /tmp/asm_ser.out /tmp/asm_mon.in /tmp/asm_mon.out

    cat /tmp/asm_ser.out > /dev/null &
    SER_PID=$!
    cat /tmp/asm_mon.out > /dev/null &
    MON_PID=$!

    timeout 17 qemu-system-i386 -drive "file=/tmp/test_floppy_${name}.img,format=raw" -display none \
        -chardev pipe,id=s,path=/tmp/asm_ser \
        -serial chardev:s \
        -chardev pipe,id=m,path=/tmp/asm_mon \
        -monitor chardev:m &
    QEMU_PID=$!

    sleep 3
    printf "asm src/%s.asm %s\r" "$name" "$out" > /tmp/asm_ser.in
    sleep 10

    kill $QEMU_PID $SER_PID $MON_PID 2>/dev/null
    wait 2>/dev/null

    # Extract output from floppy
    found=0
    for sec in 0 1; do
        base=$(( (9 + sec) * 512 ))
        for i in $(seq 0 15); do
            off=$((base + i * 32))
            entry_name=$(dd if="/tmp/test_floppy_${name}.img" bs=1 skip=$off count=27 2>/dev/null | tr -d '\000')
            if [ "$entry_name" = "$out" ]; then
                start_sec=$(dd if="/tmp/test_floppy_${name}.img" bs=1 skip=$((off + 28)) count=2 2>/dev/null | od -An -tu2 | tr -d ' ')
                file_size=$(dd if="/tmp/test_floppy_${name}.img" bs=1 skip=$((off + 30)) count=2 2>/dev/null | od -An -tu2 | tr -d ' ')
                dd if="/tmp/test_floppy_${name}.img" bs=1 skip=$(( (start_sec - 1) * 512 )) count=$file_size of="/tmp/out_${name}.bin" 2>/dev/null

                if cmp -s "$ref" "/tmp/out_${name}.bin"; then
                    printf "  PASS  %-20s %d bytes\n" "${name}.asm" "$ref_size"
                    PASS=$((PASS + 1))
                else
                    printf "  FAIL  %-20s expected %d bytes, got %d bytes\n" "${name}.asm" "$ref_size" "$file_size"
                    FAIL=$((FAIL + 1))
                    ERRORS="$ERRORS ${name}.asm"
                fi
                found=1
                break 2
            fi
        done
    done

    if [ "$found" = "0" ]; then
        printf "  FAIL  %-20s output file not found on floppy\n" "${name}.asm"
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS ${name}.asm"
    fi

    if [ -z "$ONLY" ]; then
        rm -f "/tmp/test_floppy_${name}.img" "/tmp/ref_${name}.bin" "/tmp/out_${name}.bin"
    else
        rm -f "/tmp/test_floppy_${name}.img"
    fi
done

# Cleanup
rm -f /tmp/asm_ser.in /tmp/asm_ser.out /tmp/asm_mon.in /tmp/asm_mon.out

# Summary
echo ""
echo "$PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
    echo "Failed:$ERRORS"
    exit 1
fi
