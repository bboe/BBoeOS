/* Symbol-table shape constants for the self-hosted assembler.
   cc.py bridges every ``#define`` here into a NASM ``%define`` at
   the top of the generated asm, so the same symbolic names are
   usable from both C code and the inline ``asm(\"...\")`` strings.

   The symbol table, jump-size table, and the four scratch buffers
   (line_buffer, output_buffer, source_buffer, include_source_save)
   all live in asm.c BSS.  Keeping the scratch buffers in BSS rather
   than past ``_bss_end`` means the kernel's page-aligned
   ``user_image_end = page_align(PROGRAM_BASE + binsize + bss_size)``
   covers them; an earlier layout placed them past ``_bss_end`` and
   crashed with a kernel-mode #PF whenever the buffer pages spilled
   beyond the mapped region.  Each symbol entry is a ``struct Symbol``
   (name + value + type + scope); the jump-size table is a flat
   ``char`` array, one byte per relative jump. */

#define JUMP_MAX            4096
#define SYMBOL_MAX          1706
#define SYMBOL_NAME_LENGTH  32

/* Macro table sizes.  16 macros × 16-byte names fits idt.asm's three
   macros with room to spare; the 2 KB body buffer holds the raw
   source lines between ``%macro`` and ``%endmacro``. */
#define MACRO_BODY_BUFFER_SIZE  2048
#define MACRO_MAX               16
#define MACRO_NAME_LEN          16
