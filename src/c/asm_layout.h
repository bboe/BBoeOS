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

   The symbol table and jump table live in their own ES segment
   (SYMBOL_SEGMENT) so they don't compete with segment-0 memory.
   JUMP_TABLE is the offset within that segment where pass 1's
   per-jump size-choice bitmap starts; 36 = SYMBOL_ENTRY covers
   32 name bytes + 2 value + 1 type + 1 scope. */

#define JUMP_MAX            4096
#define JUMP_TABLE          0xF000
#define SYMBOL_ENTRY        36
#define SYMBOL_MAX          1706
#define SYMBOL_NAME_LENGTH  32
#define SYMBOL_SEGMENT      0x2000

#define LINE_BUFFER         _bss_end
#define OUTPUT_BUFFER       (LINE_BUFFER + 256)
#define SOURCE_BUFFER       (OUTPUT_BUFFER + 512)
