        ;; sib_addressing_sm.asm — smoke test for the self-hosted
        ;; assembler's SIB (scale-index-base) addressing encoder.
        ;;
        ;; Before this support, ``[reg + reg]`` and ``[reg + reg*k]``
        ;; effective addresses fell through to the legacy ``[reg+disp]``
        ;; parser path: the trailing register was misread as a symbol
        ;; (``resolve_value(edx)`` → 0), producing the disp=0 ``mov
        ;; [esi], al`` byte sequence instead of the correct SIB form.
        ;; cc.py's IndexAssign-with-pinned-index codegen emits these
        ;; SIB shapes; without an assembler encoder they would either
        ;; mis-assemble silently or trip abort_unknown.
        ;;
        ;; test_asm.py diffs the in-OS assembler's output against
        ;; NASM's; byte identity is the only contract.

        [bits 32]
        org 08048000h

main:
        ;; ----- byte stores -----
        mov [esi+edx], al               ; 88 04 16
        mov [esi+edx*2], al             ; 88 04 56
        mov [esi+edx*4], al             ; 88 04 96
        mov [esi+edx*8], al             ; 88 04 d6
        mov [esi+edx+8], al             ; 88 44 16 08
        mov [esi+edx*4+8], al           ; 88 44 96 08
        mov [esi+edx*4+1000], al        ; 88 84 96 e8 03 00 00

        ;; ----- dword stores -----
        mov [esi+edx], eax              ; 89 04 16
        mov [esi+edx*2], eax            ; 89 04 56
        mov [esi+edx*4], eax            ; 89 04 96
        mov [esi+edx*4+8], eax          ; 89 44 96 08
        mov [ebp+edx*4], eax            ; 89 44 95 00 (ebp base ⇒ mod=01 disp8=0)
        mov [ebp+edx*4+8], eax          ; 89 44 95 08

        ;; ----- byte loads -----
        mov al, [esi+edx]               ; 8a 04 16
        mov al, [esi+edx*4]             ; 8a 04 96

        ;; ----- dword loads -----
        mov eax, [esi+edx]              ; 8b 04 16
        mov eax, [esi+edx*4]            ; 8b 04 96
        mov eax, [esi+edx*4+8]          ; 8b 44 96 08

        ;; ----- mixed source registers -----
        mov [edi+ecx*2], bl
        mov [ebx+esi*4], ecx
        mov [eax+edi], dl

        ;; ----- store immediates through SIB -----
        mov byte [esi+edx], 0x55
        mov dword [esi+edx*4], 0x12345678

        ret
