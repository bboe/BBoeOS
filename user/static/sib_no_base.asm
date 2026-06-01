        ;; sib_no_base.asm — smoke test for the SIB no-base
        ;; addressing form ``[disp32 + reg*scale]`` in the self-hosted
        ;; assembler.  Companion to sib_addressing_sm.asm (``mov``),
        ;; sib_addressing_alu.asm (ALU family), and
        ;; sib_addressing_lea.asm (``lea``).
        ;;
        ;; The no-base SIB form encodes as mod=00 rm=100 SIB(base=101)
        ;; disp32, leaving the base register field as the architectural
        ;; "no register" sentinel.  Used by cc.py for indexed access to
        ;; file-scope arrays: ``arr[i]`` lowers to ``[_g_arr + idx*k]``
        ;; instead of staging the scaled index through ESI.
        ;;
        ;; Restricted to scales 4 and 8: NASM canonicalizes scale=2 to
        ;; ``[base+base]`` form (a different encoding) and scale=1 to
        ;; the standard ``[reg+disp32]`` form (also different), so
        ;; matching its byte stream means avoiding those scales here.
        ;; cc.py's codegen consumer only emits the no-base SIB form for
        ;; scales 4 and 8 too, so this fixture stays representative.
        ;;
        ;; test_asm.py diffs the in-OS assembler's output against
        ;; NASM's; byte identity is the only contract.

        [bits 32]
        org 08048000h

main:
        ;; ----- loads ``[g + reg*scale]`` -----
        mov eax, [g + edx*4]            ; 8b 04 95 disp32
        mov eax, [g + esi*8]

        ;; ----- stores ``[g + reg*scale], reg`` -----
        mov [g + edx*4], eax            ; 89 04 95 disp32
        mov [g + edi*4], al             ; 88 store

        ;; ----- ALU forms with no-base SIB -----
        add [g + edx*4], eax
        sub [g + edx*4], ebx
        and dword [g + ecx*4], 0xFF
        xor dword [g + edx*4], -1
        cmp dword [g + edx*4], 0
        inc dword [g + edx*4]           ; ff 04 95 disp32
        test byte [g + edx*4], 1

        ;; ----- lea with no-base SIB -----
        lea eax, [g + edx*4]
        lea ebx, [g + ecx*4 + 8]

        ;; ----- movzx byte load -----
        movzx eax, byte [g + edx*4]

        ;; ----- trailing disp (label + reg*k + N) -----
        mov eax, [g + edx*4 + 12]
        mov eax, [g + edi*4 + 1000]

        ret

g:      times 16 dd 0
