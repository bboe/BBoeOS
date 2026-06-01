        ;; sib_addressing_alu.asm — smoke test for ALU memory operands
        ;; with SIB (scale-index-base) addressing in the self-hosted
        ;; assembler.  Companion to sib_addressing_sm.asm which covers
        ;; the ``mov`` SIB shapes.
        ;;
        ;; Without this support, ``peephole_memory_arithmetic`` and
        ;; cc.py's in-place-update lowering can't emit
        ;; ``add dword [esi+edx*4], 1`` and the like — the assembler
        ;; would mis-parse the trailing register as a symbol and emit
        ;; the wrong byte stream.
        ;;
        ;; test_asm.py diffs the in-OS assembler's output against
        ;; NASM's; byte identity is the only contract.

        [bits 32]
        org 08048000h

main:
        ;; ----- <op> [mem], r8  (00 /r) -----
        add [esi+edx], al
        and [esi+edx*4], bl
        or  [esi+edx*2+8], cl
        sub [edi+ecx*4], dl
        xor [ebx+esi*8+1000], al

        ;; ----- <op> [mem], r32 (01 /r, bits=32) -----
        add [esi+edx], eax
        and [esi+edx*4], ebx
        or  [esi+edx*2+8], ecx
        sub [edi+ecx*4+200], edx
        xor [eax+edi], ebx

        ;; ----- <op> r8, [mem] (02 /r) -----
        add al, [esi+edx]
        and bl, [esi+edx*4]
        sub cl, [esi+edx+8]
        xor dl, [edi+ecx*2+1000]

        ;; ----- <op> r32, [mem] (03 /r, bits=32) -----
        add eax, [esi+edx]
        and ebx, [esi+edx*4]
        sub ecx, [esi+edx+8]
        xor edx, [eax+edi*4+200]

        ;; ----- cmp reg, [mem] (3A /r, 3B /r) -----
        cmp al, [esi+edx]
        cmp ecx, [esi+edx*4+8]

        ;; ----- <op> byte [mem], imm8 (80 /r ib) -----
        add byte [esi+edx], 0x55
        and byte [esi+edx*4], 0xF0
        or  byte [esi+edx*2+8], 1
        sub byte [edi+ecx], 7
        xor byte [esi+edx*8+1000], 0xAA
        cmp byte [esi+edx], 0

        ;; ----- <op> dword [mem], imm8 sign-ext (83 /r ib) -----
        add dword [esi+edx], 1
        and dword [esi+edx*4], -1
        sub dword [esi+edx+8], 4
        xor dword [edi+ecx], 16
        cmp dword [esi+edx*4+8], 100

        ;; ----- <op> dword [mem], imm32 (81 /r id) -----
        add dword [esi+edx], 0x1000
        and dword [esi+edx*4], 0x12345678
        sub dword [esi+edx+8], 0xDEAD
        cmp dword [esi+edx*4+8], 0xDEADBEEF

        ;; ----- ebp-base special case (mod=01 disp8=0) -----
        add dword [ebp+edx*4], eax
        cmp dword [ebp+edx], 1
        sub byte [ebp+esi], al

        ;; ----- test byte [mem], imm8 (F6 /0 ib) -----
        test byte [esi+edx], 1
        test byte [esi+edx*4], 0x80
        test byte [esi+edx*2+8], 0xFF
        test byte [ebp+edx], 0x40

        ret
