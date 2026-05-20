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

        org 08048000h

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

        ;; mov r32, [mem] / mov [mem], r32 — direct-memory moves.
        ;; Under bits=32 the address operand widens to disp32 and the
        ;; ModR/M rm field flips from 110 to 101.  Both the accumulator
        ;; short form (A0/A1/A2/A3) and the generic /r form exercise.
        mov eax, [gdt_desc]
        mov ebx, [gdt_desc]
        mov [gdt_desc], eax
        mov [gdt_desc], ebx
        mov ax, [gdt_desc]
        mov [gdt_desc], ax

        ;; [reg32] / [reg32+disp] addressing — phase 5.6.  Under
        ;; bits=16 the 0x67 address-size prefix flips addressing to
        ;; 32-bit; ESP / EBP require SIB and disp8=0 quirks that
        ;; the new emit_indexed_mem handles.  Each form below is
        ;; byte-diffed against NASM.
        mov al, [esp]
        mov al, [esp+4]
        mov eax, [esp]
        mov eax, [esp+8]
        mov eax, [ebp]
        mov eax, [ebp+12]
        mov eax, [ebx]
        mov eax, [ebx+16]
        mov [esp], eax
        mov [esp+4], eax
        mov [ebx], eax
        add eax, [esp]
        add eax, [ebx+4]
        cmp dword [ebx], 0
        cmp dword [ebx+4], 0x1000
        test byte [ebx+4], 0x80
        inc dword [ebx]
        inc dword [ebx+4]
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

        ;; [reg32+disp] under bits=16 picks up the 0x67 address-size
        ;; prefix on every form; mirror the bits=32 block so NASM
        ;; diff stays honest.
        mov al, [esp]
        mov eax, [esp+4]
        mov eax, [ebp]
        mov eax, [ebx+8]
        mov [esp], eax
        mov [ebx], eax
        add eax, [esp]
        cmp dword [ebx], 0
        inc dword [ebx+4]

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
