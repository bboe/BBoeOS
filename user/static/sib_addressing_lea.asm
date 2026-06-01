        ;; sib_addressing_lea.asm — smoke test for ``lea`` with SIB
        ;; (scale-index-base) addressing in the self-hosted assembler.
        ;; Companion to sib_addressing_sm.asm (``mov``) and
        ;; sib_addressing_alu.asm (ALU family).
        ;;
        ;; ``lea`` is the cheapest way to fold a base + scaled-index +
        ;; displacement into a single instruction; without SIB support
        ;; here, ``&arr[i]`` / pointer-arithmetic codegen has to lower
        ;; to a three-instruction sequence.  test_asm.py diffs the
        ;; in-OS assembler's output against NASM's; byte identity is
        ;; the only contract.

        [bits 32]
        org 08048000h

main:
        ;; ----- lea reg, [base + index*scale] -----
        lea eax, [esi+edx]
        lea ebx, [esi+edx*2]
        lea ecx, [esi+edx*4]
        lea edx, [esi+edi*8]

        ;; ----- lea reg, [base + index*scale + disp8] -----
        lea eax, [esi+edx+8]
        lea ebx, [esi+edx*4+8]
        lea ecx, [edi+ecx*2+1]
        lea edx, [ebx+esi*8-128]

        ;; ----- lea reg, [base + index*scale + disp32] -----
        lea eax, [esi+edx+1000]
        lea ebx, [esi+edx*4+1000]
        lea ecx, [edi+ecx*8+0x12345678]

        ;; ----- ebp-base special case (mod=01 disp8=0) -----
        lea eax, [ebp+edx]
        lea ebx, [ebp+edx*4]
        lea ecx, [ebp+esi*8+4]

        ;; ----- esp-as-base (legal since esp can be base) -----
        lea eax, [esp+edx]
        lea ebx, [esp+edx*4]
        lea ecx, [esp+edx*2+16]

        ret
