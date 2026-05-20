#!/bin/sh

FS_TYPE=bbfs
IMAGE=drive.img
EXT2_BLOCK_SIZE=1024
EXT2_INODE_COUNT=
SECTORS=2880        # default = 1.44 MB floppy size; override for larger images
WITH_TEST_PROGRAMS=0
for arg in "$@"; do
    case "$arg" in
        --ext2) FS_TYPE=ext2 ;;
        --bbfs) FS_TYPE=bbfs ;;
        --ext2-block-size=*) EXT2_BLOCK_SIZE="${arg#*=}" ;;
        --ext2-inode-count=*) EXT2_INODE_COUNT="${arg#*=}" ;;
        --sectors=*) SECTORS="${arg#*=}" ;;
        --with-test-programs) WITH_TEST_PROGRAMS=1 ;;
        -*) echo "Unknown flag: $arg" >&2; exit 1 ;;
        *) IMAGE="$arg" ;;
    esac
done

KBUILD=build/kernel-c
rm -rf "$KBUILD" && mkdir -p "$KBUILD"
find kernel -name '*.c' | while read -r source; do
    rel="${source#kernel/}"; out="$KBUILD/${rel%.c}.kasm"
    mkdir -p "$(dirname "$out")"
    python3 cc.py --bits 32 --target kernel "$source" "$out" || exit 1
done

# Build the libbboeos blob (FUNCTION_TABLE + shared_* helpers).  Lives at
# lib/libbboeos on the disk image; libbboeos_install reads it from disk at
# boot, copies it into a freshly-allocated frame, and maps that frame
# (with PTE_SHARED) at user-virt FUNCTION_TABLE (0x00010000) in every
# per-program PD.  Layout of build/libbboeos matches the in-memory page:
# trampolines + helper bodies at offset 0, sigreturn trampoline at
# offset 0x460, FUNCTION_POINTER_TABLE at offset 0x800.  The linker
# script (user/libbboeos/libbboeos.ld) places each section at its
# anchor; objcopy -O binary flattens the linked ELF into the on-disk
# blob.
mkdir -p build
make -C user/libbboeos libbboeos.a >/dev/null || exit 1
nasm -f elf32 -i kernel/include/ -o build/libbboeos.o user/libbboeos/libbboeos.asm || exit 1
# --gc-sections drops every clang-compiled libbboeos function the
# pointer-table LONG()s don't reference, so the blob pays only for
# what it actually exports.
ld -m elf_i386 -T user/libbboeos/libbboeos.ld -Map=build/libbboeos.map \
    --gc-sections -o build/libbboeos.elf \
    build/libbboeos.o user/libbboeos/libbboeos.a || exit 1
objcopy -O binary build/libbboeos.elf build/libbboeos || exit 1

# Two-pass build: assemble kernel.bin first, measure its sector count,
# then pass that as -DKERNEL_SECTORS=N when assembling boot.bin so the
# real-mode INT 13h knows how many sectors of kernel.bin to load into
# physical 0x10000.  boot.bin pads itself to a fixed BOOT_SECTORS so
# KERNEL_SECTORS is the only build-time variable; if boot.bin overflows
# the pad, NASM's `times` directive goes negative and the build fails.
nasm -f bin \
    -i kernel/include/ -i kernel/ -i kernel/arch/x86/ -i kernel/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" -i "$KBUILD/arch/x86/" -i "$KBUILD/syscall/" \
    -o kernel.bin kernel/arch/x86/kernel.asm
if [ $? -ne 0 ]; then
    exit 1
fi

KERNEL_SIZE=$(wc -c < kernel.bin)
KERNEL_SECTORS=$(( ( KERNEL_SIZE + 511 ) / 512 ))

# Compute the first page above the kernel's *resident extent* — that's
# .text/.data on-disk size PLUS the .bss extent.  The BSS section is
# `nobits` (declared in kernel/arch/x86/kernel.asm), so its bytes don't
# ride on disk; we read its size from build/kernel.map (emitted by
# the [map symbols] directive at the end of kernel.asm).  Without
# this, KERNEL_RESERVED_BASE would land inside the BSS region and
# the kernel stack would overlap program_state_a / tss_data etc.
# KERNEL_LOAD_PHYS = 0x20000 (must match boot.asm + kernel.asm).
#
# Map row format: leading whitespace, two identical hex address
# columns (no 0x prefix), then the symbol name.
BSS_START=$(awk '$NF=="kernel_bss_start"{print $1; exit}' build/kernel.map)
BSS_END=$(awk '$NF=="kernel_bss_end"{print $1; exit}' build/kernel.map)
if [ -z "$BSS_START" ] || [ -z "$BSS_END" ]; then
    echo "make_os.sh: failed to read kernel_bss_{start,end} from build/kernel.map" >&2
    exit 1
fi
BSS_BYTES=$(( 0x$BSS_END - 0x$BSS_START ))

KERNEL_RESERVED_BASE=$(( (0x20000 + KERNEL_SIZE + BSS_BYTES + 0xFFF) & ~0xFFF ))

# Safety: the entire kernel-side reserved region must stay below the
# VGA aperture at phys 0xA0000.  Worst-case region size:
#   3 × KERNEL_STACK_BYTES         =   3 KB (slot_a + slot_b + slot_c, 1 KB each)
#   + BOOT_PD                      =   4 KB
#   + FIRST_KERNEL_PT              =   4 KB
#   + frame_bitmap (worst case)    = 128 KB (1M frames at -m 4096 / 8 bits/byte;
#                                            sized at boot from E820, capped at
#                                            FRAME_PHYSICAL_LIMIT ~ 4 GB)
#   = 139 KB, rounded up to a page = 0x23000.
# Keeping the whole region under 0xA0000 lets the OS boot under QEMU
# `-m 1` (1 MB total), where conventional RAM ends at 0x9FC00.  On
# `-m 1` the bitmap is actually ~20 bytes, so the runtime region is
# much smaller than the worst case checked here.
KERNEL_RESERVED_END=$(( KERNEL_RESERVED_BASE + 0x23000 ))
if [ $KERNEL_RESERVED_END -ge $(( 0xA0000 )) ]; then
    echo "make_os.sh: kernel reserved region (ends at $(printf 0x%x $KERNEL_RESERVED_END)) crosses the VGA hole at 0xA0000" >&2
    exit 1
fi

# Second pass: re-assemble kernel.bin with the correct layout anchor.
# All immediates are 32-bit regardless of value, so the size is invariant.
nasm -f bin \
    -DKERNEL_RESERVED_BASE=$KERNEL_RESERVED_BASE \
    -i kernel/include/ -i kernel/ -i kernel/arch/x86/ -i kernel/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" -i "$KBUILD/arch/x86/" -i "$KBUILD/syscall/" \
    -o kernel.bin kernel/arch/x86/kernel.asm
if [ $? -ne 0 ]; then
    exit 1
fi

KERNEL_SIZE_2=$(wc -c < kernel.bin)
if [ "$KERNEL_SIZE_2" -ne "$KERNEL_SIZE" ]; then
    echo "make_os.sh: kernel.bin size changed between passes ($KERNEL_SIZE → $KERNEL_SIZE_2)" >&2
    exit 1
fi

nasm -f bin \
    -DKERNEL_SECTORS=$KERNEL_SECTORS \
    -DKERNEL_RESERVED_BASE=$KERNEL_RESERVED_BASE \
    -i kernel/include/ -i kernel/ -i kernel/arch/x86/ -i kernel/arch/x86/boot/ \
    -i "$KBUILD/" -i "$KBUILD/net/" -i "$KBUILD/fs/" \
    -o boot.bin kernel/arch/x86/boot/boot.asm
if [ $? -ne 0 ]; then
    exit 1
fi

cat boot.bin kernel.bin > os.bin

# User-facing programs are every user/programs/*.c file; test-only fixtures
# live under tests/programs/ and only ship in the drive image when
# --with-test-programs is passed (default builds keep them out so a
# normal boot has just the user programs).  Both lists are sorted so
# the on-disk directory layout is stable across builds.
# A program named <name> with sibling translation units lists those
# extras in tests/programs/<name>.deps (one filename per line,
# relative to the directory holding <name>.c).  Listed sources are
# pulled into the link of <name> and excluded from the auto-
# discovered program lists below so they do not also build as their
# own programs.  Today only the multitu_demo test uses this.
MULTI_TU_DEPS_HELPERS=$(find user/programs tests/programs -maxdepth 1 -name '*.deps' 2>/dev/null | while read -r deps_path; do
    while read -r helper; do
        [ -z "$helper" ] && continue
        echo "${helper%.c}"
    done < "$deps_path"
done | sort -u | tr '\n' ' ')

exclude_helpers() {
    result=""
    for candidate in $1; do
        keep=1
        for helper in $MULTI_TU_DEPS_HELPERS; do
            if [ "$candidate" = "$helper" ]; then
                keep=0
                break
            fi
        done
        [ $keep -eq 1 ] && result="$result $candidate"
    done
    echo "$result"
}

USER_PROGRAMS_RAW=$(find user/programs -maxdepth 1 -name '*.c' | sed 's|.*/||; s/\.c$//' | sort | tr '\n' ' ')
TEST_PROGRAMS_RAW=$(find tests/programs -maxdepth 1 -name '*.c' | sed 's|.*/||; s/\.c$//' | sort | tr '\n' ' ')
USER_PROGRAMS=$(exclude_helpers "$USER_PROGRAMS_RAW")
TEST_PROGRAMS=$(exclude_helpers "$TEST_PROGRAMS_RAW")
if [ "$WITH_TEST_PROGRAMS" -eq 1 ]; then
    PROGRAMS="$USER_PROGRAMS $TEST_PROGRAMS"
else
    PROGRAMS="$USER_PROGRAMS"
fi

PBUILD=build/c
rm -rf "$PBUILD" && mkdir -p "$PBUILD"

# cc.py user programs route through the object-file + linker pipeline
#     cc.py --object → nasm -f bin -l → cc.py pack-ccobj → ccld.py
# by default.  Programs whose names appear in FLAT_PROGRAMS stay on
# the legacy single-TU flat path (cc.py → nasm -f bin) — reserved for
# escape hatches if some future emit pattern hits a pipeline bug
# before the matching ccobj/ccld fix lands.  Currently empty: every
# program in user/programs/ + tests/programs/ builds cleanly through the
# linker.  Either path produces a flat binary loadable by
# program_enter (same PROGRAM_BASE, same BSS trailer), so the shell
# and runtime ABI don't change with the toolchain choice.
FLAT_PROGRAMS="arp asm dns shell"

compile_program_flat() {
    name=$1
    source=$2
    python3 cc.py --bits 32 "$source" "$PBUILD/$name.asm" || return 1
    nasm -f bin -i kernel/include/ -o "$PBUILD/$name" "$PBUILD/$name.asm" || return 1
}

compile_program_object() {
    name=$1
    source=$2
    source_directory=$(dirname "$source")
    deps_file="${source%.c}.deps"
    # Compile and pack the main translation unit first.  When a
    # <name>.deps sidecar exists, walk each listed source through
    # the same cc.py --object → nasm → pack-ccobj pipeline and
    # extend the linker's input list before invoking ccld.
    python3 cc.py --bits 32 --object "$source" "$PBUILD/$name.asm" || return 1
    nasm -f bin -i kernel/include/ -l "$PBUILD/$name.lst" -o "$PBUILD/$name.obin" "$PBUILD/$name.asm" || return 1
    python3 cc.py pack-ccobj "$PBUILD/$name.obin" "$PBUILD/$name.lst" "$PBUILD/$name.ccobj" || return 1
    objects="$PBUILD/$name.ccobj"
    if [ -f "$deps_file" ]; then
        while read -r helper; do
            [ -z "$helper" ] && continue
            helper_source="$source_directory/$helper"
            helper_stem=$(basename "$helper" .c)
            python3 cc.py --bits 32 --object "$helper_source" "$PBUILD/$helper_stem.asm" || return 1
            nasm -f bin -i kernel/include/ -l "$PBUILD/$helper_stem.lst" -o "$PBUILD/$helper_stem.obin" "$PBUILD/$helper_stem.asm" || return 1
            python3 cc.py pack-ccobj "$PBUILD/$helper_stem.obin" "$PBUILD/$helper_stem.lst" "$PBUILD/$helper_stem.ccobj" || return 1
            objects="$objects $PBUILD/$helper_stem.ccobj"
        done < "$deps_file"
    fi
    python3 tools/ccld.py --output "$PBUILD/$name" $objects || return 1
}

is_flat_program() {
    case " $FLAT_PROGRAMS " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

for name in $USER_PROGRAMS; do
    if is_flat_program "$name"; then
        compile_program_flat "$name" "user/programs/$name.c" || exit 1
    else
        compile_program_object "$name" "user/programs/$name.c" || exit 1
    fi
done
if [ "$WITH_TEST_PROGRAMS" -eq 1 ]; then
    for name in $TEST_PROGRAMS; do
        if is_flat_program "$name"; then
            compile_program_flat "$name" "tests/programs/$name.c" || exit 1
        else
            compile_program_object "$name" "tests/programs/$name.c" || exit 1
        fi
    done
fi

dd bs=512 count="$SECTORS" if=/dev/zero of="$IMAGE"
dd conv=notrunc if=os.bin of="$IMAGE"

if [ "$FS_TYPE" = "ext2" ]; then
    EXT2_START=$(python3 -c "from add_file import compute_directory_sector; print(compute_directory_sector(image_path='$IMAGE'))") || exit 1
    mke2fs -b "$EXT2_BLOCK_SIZE" -t ext2 -m 0 -E offset=$((EXT2_START * 512)) ${EXT2_INODE_COUNT:+-N "$EXT2_INODE_COUNT"} "$IMAGE" $(( (SECTORS - EXT2_START) / 2 )) || exit 1
fi

./add_file.py --mkdir --image "$IMAGE" bin || exit 1
PROGRAM_PATHS=""
for name in $PROGRAMS; do
    PROGRAM_PATHS="$PROGRAM_PATHS $PBUILD/$name"
done
./add_file.py -x -d bin --image "$IMAGE" $PROGRAM_PATHS || exit 1

# Shared-library blob — libbboeos_install reads `lib/libbboeos` from disk
# at boot and maps it (with PTE_SHARED) at user-virt FUNCTION_TABLE
# (0x00010000) in every per-program PD.
./add_file.py --mkdir --image "$IMAGE" lib || exit 1
./add_file.py -d lib --image "$IMAGE" build/libbboeos || exit 1

# Static reference files used by cat / cp / asm tests.
./add_file.py --mkdir --image "$IMAGE" src || exit 1
STATIC_FILES=""
for f in user/static/*; do
    [ -f "$f" ] && STATIC_FILES="$STATIC_FILES $f"
done
if [ -n "$STATIC_FILES" ]; then
    ./add_file.py -d src --image "$IMAGE" $STATIC_FILES || exit 1
fi
