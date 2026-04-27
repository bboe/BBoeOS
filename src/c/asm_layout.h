/* Memory layout and symbol-table shape constants for the self-hosted
   assembler.  cc.py bridges every ``#define`` here into a NASM
   ``%define`` at the top of the generated asm, so the same symbolic
   names are usable from both C code and the inline ``asm(\"...\")``
   strings.

   Scratch-buffer layout lives past ``_bss_end`` (= ``_program_end`` +
   BSS size): the line buffer (256 bytes), the output buffer (512 bytes),
   and the source-read buffer (512 bytes).  A further 512-byte block
   starting at ``_bss_end + 1280`` holds the parent's source-buffer copy
   for ``%include`` nesting (main() initializes
   ``include_source_save`` to that address).

   The symbol and jump tables live in extended memory at SYMBOL_BASE
   (3 MB mark, well clear of the kernel and edit's gap buffer).  Flat
   pmode addressing reaches anywhere in 4 GB so the previous segmented
   ES-window scheme retired with the 16-bit port — every far-memory
   access is now a plain 32-bit load.  JUMP_TABLE is the flat address
   where pass 1's per-jump size-choice bitmap starts; SYMBOL_ENTRY
   (38) covers 32 name bytes + 4 value + 1 type + 1 scope.  The
   value field is dword-wide so symbols whose value exceeds 16 bits
   (``%define JUMP_TABLE = SYMBOL_BASE + 0xF000`` = 0x30F000 is the
   canonical case) round-trip cleanly. */

#define JUMP_MAX            4096
#define SYMBOL_BASE         0x300000
#define JUMP_TABLE          (SYMBOL_BASE + 0xF000)
#define SYMBOL_ENTRY        38
#define SYMBOL_MAX          1706
#define SYMBOL_NAME_LENGTH  32

#define LINE_BUFFER         _bss_end
#define OUTPUT_BUFFER       (LINE_BUFFER + 256)
#define SOURCE_BUFFER       (OUTPUT_BUFFER + 512)

/* Macro table sizes.  16 macros × 16-byte names fits idt.asm's three
   macros with room to spare; the 2 KB body buffer holds the raw
   source lines between ``%macro`` and ``%endmacro``. */
#define MACRO_BODY_BUFFER_SIZE  2048
#define MACRO_MAX               16
#define MACRO_NAME_LEN          16
