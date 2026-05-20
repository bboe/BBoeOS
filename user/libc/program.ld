/* tools/libc/program.ld — flat binary at PROGRAM_BASE for BBoeOS
 * userland programs linked against libbboeos.a.
 *
 * Emits .text + .rodata + .data starting at PROGRAM_BASE = 0x08048000,
 * then a 6-byte BSS trailer at the very end of the file: a 32-bit
 * little-endian BSS size followed by the magic word 0xB032.  The
 * existing bboeos program loader (program_enter in entry.asm) seeks
 * to file_size - 6, reads the trailer, and maps that many zero-filled
 * pages above the loaded image.  Format matches what cc.py emits for
 * its own programs (cc/codegen/x86/generator.py:417 _emit_bss_trailer).
 *
 * No bytes are emitted into the binary for .bss itself — __bss_start
 * and __bss_end are linker-supplied symbols that _start.S uses to
 * zero the BSS region on entry. */

PROGRAM_BASE = 0x08048000;
ENTRY(_start)

SECTIONS {
    . = PROGRAM_BASE;

    .text   : { *(.text .text.*) }
    .rodata : ALIGN(16) { *(.rodata .rodata.*) }
    .data   : ALIGN(16) { *(.data   .data.*) }

    __bss_start = .;
    .bss (NOLOAD) : ALIGN(16) {
        *(.bss .bss.*)
        *(COMMON)
    }
    __bss_end = .;

    /* BSS trailer: dd <bss_size>; dw 0xB032.  6 bytes flush against
     * the end of the loaded image so file_size - 6 hits the dword. */
    .bss_trailer : {
        LONG(__bss_end - __bss_start);
        SHORT(0xB032);
    }

    /DISCARD/ : {
        *(.note*)
        *(.comment)
        *(.eh_frame*)
    }
}
