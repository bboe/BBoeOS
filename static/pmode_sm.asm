        ;; pmode_sm.asm — protected-mode smoke test for the self-hosted
        ;; assembler.  Exercises the pmode-specific encodings added in
        ;; phase 5: 32-bit general registers, control registers, lgdt /
        ;; lidt, and the 0x66-prefixed far jmp with a dword offset.
        ;;
        ;; The bytes are meaningless as a program — the file is never
        ;; run.  test_asm.py diffs asm.c's output against NASM's; byte
        ;; identity is the only contract.  Immediates stay inside 16
        ;; bits so cc.py's current bits=16 integer width can round-trip
        ;; them; widening comes in phase 4b.

        org 0600h

        mov eax, cr0
        or  eax, 1
        mov cr0, eax
        mov eax, 0x1234
        mov ebx, eax
        lgdt [gdt_desc]
        lidt [idt_desc]
        jmp dword 0x08:pm_entry
pm_entry:
        jmp pm_entry

        ;; [bits 32] flips the default operand size.  Same mnemonics,
        ;; different prefix pattern: 32-bit ops emit bare opcodes, and
        ;; 16-bit ops acquire the 0x66 prefix.  NASM's own encoder
        ;; behaves identically, so byte-for-byte diff still holds.
[bits 32]
pm32:
        mov eax, cr0
        or  eax, 1
        mov cr0, eax
        mov eax, 0x1234
        mov ebx, eax
        jmp dword 0x08:pm_entry_32
pm_entry_32:
        mov ax, 0x1234
        or  ax, 1
        ;; 32-bit push / pop under bits=32 emit bare 0x50+reg / 0x58+reg;
        ;; bits=16 sibling requires a 0x66 operand-size prefix.  Mirror
        ;; the pattern with 16-bit push under bits=32 (0x66-prefixed) so
        ;; both directions are covered.
        push eax
        push ecx
        pop  edx
        pop  ebx
        push ax
        pop  bx
        ;; push <size> imm — size forces the push width.  imm8 short
        ;; form (0x6A ib) still applies when the value fits ±128
        ;; regardless of operand size; only the prefix reflects the
        ;; push width.
        push dword 0
        push dword 0x1234
        push word 0
        push word 0x1234
[bits 16]
pm16_back:
        mov ax, 0x5678
        push eax
        push ax
        pop  ecx
        pop  bx
        push dword 0
        push dword 0x1234
        push 0x1234

        ;; align N pads current_address to a multiple of N with zero
        ;; bytes — exercises the STR_ALIGN directive added in phase 5.2.
        align 8
gdt_desc:
        dw 0
        dd 0
        align 4
idt_desc:
        dw 0
        dd 0
