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

gdt_desc:
        dw 0
        dd 0
idt_desc:
        dw 0
        dd 0
