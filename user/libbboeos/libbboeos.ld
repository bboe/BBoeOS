/* user/libbboeos/libbboeos.ld — flat binary at user-virt 0x00010000 for
 * BBoeOS's shared libbboeos blob (vDSO + future C-compiled helpers).
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
 *   0x10460  .libbboeos.sigreturn        __kernel_sigreturn trampoline
 *                                        (matches VDSO_SIGRETURN_OFFSET).
 *   0x10800  .libbboeos.pointer_table    FUNCTION_POINTER_TABLE — 13
 *                                        absolute LONG(shared_*) entries
 *                                        the linker resolves at link
 *                                        time, replacing the previous
 *                                        gen_libbboeos_pointers.py +
 *                                        python-concat dance.
 *
 * Future libbboeos C bodies (Phase 3+) land in .libbboeos.text, growing
 * the helper region until it bumps into the 0x10460 sigreturn anchor.
 * objcopy -O binary flattens the linked ELF into build/libbboeos, which
 * make_os.sh adds to the disk image as `lib/libbboeos`. */

ENTRY(function_table)

SECTIONS {
    . = 0x00010000;

    .libbboeos.function_table : { KEEP(*(.libbboeos.function_table)) }
    .libbboeos.text           : { *(.libbboeos.text) }
    .libbboeos.rodata         : { *(.libbboeos.rodata) }

    . = 0x00010460;
    .libbboeos.sigreturn      : { KEEP(*(.libbboeos.sigreturn)) }

    . = 0x00010800;
    .libbboeos.pointer_table  : {
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
    }

    /DISCARD/ : {
        *(.note*)
        *(.comment)
        *(.eh_frame*)
    }
}
