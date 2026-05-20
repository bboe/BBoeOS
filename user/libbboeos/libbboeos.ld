/* user/libbboeos/libbboeos.ld — flat binary at user-virt 0x00010000 for
 * BBoeOS's shared libbboeos blob (libbboeos + future C-compiled helpers).
 *
 * Anchored at 0x10000 to match the FUNCTION_TABLE constant in
 * kernel/include/constants.asm.  Layout, in order:
 *
 *   0x10000  .libbboeos.function_table   13 × 5-byte `jmp strict near`
 *                                        slots (65 bytes); first byte is
 *                                        FUNCTION_DIE.
 *   0x10046  .libbboeos.text             shared_* helper bodies (ported
 *                                        from src/lib/{proc,print}.asm).
 *   ...      .libbboeos.rodata           print_datetime_month_lengths.
 *   0x10E00  .libbboeos.pointer_table    FUNCTION_POINTER_TABLE.  13
 *                                        legacy LONG(shared_*) entries
 *                                        + a growing tail of clang-
 *                                        compiled libbboeos exports.
 *                                        Entry order MUST match the
 *                                        FUNCTION_*_PTR offsets in
 *                                        kernel/include/constants.asm.
 *   0x10FE0  .libbboeos.sigreturn        __kernel_sigreturn trampoline
 *                                        (matches LIBBBOEOS_SIGRETURN_OFFSET).
 *
 * Clang-compiled libbboeos C bodies (string.c, etc.) land in
 * .libbboeos.text via `*(.text .text.*)`; -ffunction-sections + ld
 * --gc-sections drop any function the LONG()s below don't reference,
 * so only what the blob actually exports gets paid for.  objcopy -O
 * binary flattens the linked ELF into build/libbboeos, which
 * make_os.sh adds to the disk image as `lib/libbboeos`. */

ENTRY(function_table)

/* Force ld to pull these archive members in (and seed --gc-sections).
 * Without EXTERN, the LONG() references inside SECTIONS aren't enough
 * to trigger archive resolution.  Sorted alphabetically. */
EXTERN(memchr)
EXTERN(memcmp)
EXTERN(memcpy)
EXTERN(memmove)
EXTERN(memset)
EXTERN(strcasecmp)
EXTERN(strcat)
EXTERN(strchr)
EXTERN(strcmp)
EXTERN(strcpy)
EXTERN(strdup)
EXTERN(strerror)
EXTERN(strlen)
EXTERN(strncasecmp)
EXTERN(strncat)
EXTERN(strncmp)
EXTERN(strncpy)
EXTERN(strrchr)
EXTERN(strstr)

SECTIONS {
    . = 0x00010000;

    .libbboeos.function_table : { KEEP(*(.libbboeos.function_table)) }
    .libbboeos.text           : {
        *(.libbboeos.text)
        *(.text .text.*)
    }
    .libbboeos.rodata         : {
        *(.libbboeos.rodata)
        *(.rodata .rodata.*)
    }

    . = 0x00010E00;
    .libbboeos.pointer_table  : {
        /* Legacy libbboeos helpers (asm).  Order pinned by FUNCTION_*_PTR in
         * kernel/include/constants.asm — entries here align 1:1. */
        LONG(shared_die);
        LONG(shared_exit);
        LONG(shared_get_character);
        LONG(shared_print_byte_decimal);
        LONG(shared_print_character);
        LONG(shared_print_datetime);
        LONG(shared_print_decimal);
        LONG(shared_print_hex);
        LONG(shared_print_ip);
        LONG(shared_print_mac);
        LONG(shared_print_string);
        LONG(shared_printf);
        LONG(shared_write_stdout);
        /* Clang-compiled libbboeos exports.  Sorted alphabetically.
         * make_os.sh rebuilds the whole user-space on every run, so
         * reordering is free until BBoeOS starts distributing binaries
         * independently of the kernel build.  Order must match the
         * FUNCTION_<name>_PTR offsets in kernel/include/constants.asm. */
        LONG(memchr);
        LONG(memcmp);
        LONG(memcpy);
        LONG(memmove);
        LONG(memset);
        LONG(strcasecmp);
        LONG(strcat);
        LONG(strchr);
        LONG(strcmp);
        LONG(strcpy);
        LONG(strdup);
        LONG(strerror);
        LONG(strlen);
        LONG(strncasecmp);
        LONG(strncat);
        LONG(strncmp);
        LONG(strncpy);
        LONG(strrchr);
        LONG(strstr);
    }

    . = 0x00010FE0;
    .libbboeos.sigreturn      : { KEEP(*(.libbboeos.sigreturn)) }

    /DISCARD/ : {
        *(.note*)
        *(.comment)
        *(.eh_frame*)
    }
}
