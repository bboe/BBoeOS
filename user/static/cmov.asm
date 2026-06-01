        ;; cmov.asm — smoke test for the cmov<cc> family in the
        ;; self-hosted assembler.  cc.py uses cmov to collapse simple
        ;; ternaries (``x = cond ? a : b``) into a branchless ``mov acc,
        ;; b / cmov<cc> acc, a`` sequence; the assembler needs to
        ;; encode each cmov<cc> mnemonic byte-identically to NASM.
        ;;
        ;; Encoding: ``0F 4X /r`` where X is the architectural
        ;; condition code (4=e, 5=ne, C=l, D=ge, E=le, F=g).  Each
        ;; cmov<cc> handler dispatches through the shared
        ;; ``cmov_handler`` helper that emits the ModR/M for the
        ;; register / r/m source.

        [bits 32]
        org 08048000h

main:
        ;; ----- register-to-register -----
        cmove eax, ecx
        cmovne edx, ebx
        cmovl esi, edi
        cmovge ebx, esi
        cmovle ecx, edx
        cmovg edi, eax

        ;; ----- register-from-memory (direct disp32) -----
        cmove eax, [g]
        cmovne edx, [g+4]

        ;; ----- register-from-memory (indexed) -----
        cmove eax, [esi+8]
        cmovne edx, [edi-4]

        ;; ----- register-from-memory (SIB) -----
        cmove eax, [esi+edx*4]
        cmovge ebx, [esi+ecx*4+8]

        ;; ----- aliases: cmovz == cmove, cmovnz == cmovne -----
        cmovz  eax, ecx
        cmovnz edx, ebx

        ret

g:      dd 0, 0
