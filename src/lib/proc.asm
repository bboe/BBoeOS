;;; ---------------------------------------------------------------------
;;; proc.asm — shared kernel helpers jumped to from user programs via the
;;; FUNCTION_TABLE (see arch/x86/boot/bboeos.asm).  The table sits at
;;; 0x7E00 with 5-byte `jmp strict near` slots; entries here define the
;;; targets.
;;; ---------------------------------------------------------------------

shared_die:
        ;; SI = message, CX = length (cc.py's `jmp FUNCTION_DIE` shape).
        ;; Writes to stdout, then falls through to shared_exit — never
        ;; returns.  Mirrors the 16-bit `call shared_write_stdout; fall
        ;; through` so cc.py's terminal printf optimisation works under
        ;; the pmode kernel without changes.
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        ;; Fall through.

shared_exit:
        ;; SYS_EXIT never returns.  The kernel restores its own ESP from
        ;; shell_esp and jumps wherever it pleases (the echo loop today,
        ;; eventually `shell_reload` to respawn the shell), so the stack
        ;; state at the moment of `int 30h` doesn't matter.  Programs
        ;; can `jmp FUNCTION_EXIT` from any call depth and be cleanly
        ;; torn down.
        mov ah, SYS_SYS_EXIT
        int 30h

shared_get_character:
        ;; Read one byte from stdin via SYS_IO_READ and return it in AL.
        ;; Preserves EBX, ECX, EDI.
        push ebx
        push ecx
        push edi
        mov bx, STDIN
        mov edi, SECTOR_BUFFER
        mov ecx, 1
        mov ah, SYS_IO_READ
        int 30h
        mov al, [SECTOR_BUFFER]
        pop edi
        pop ecx
        pop ebx
        ret

shared_parse_argv:
        ;; Split [EXEC_ARG] (a dword pointer to the raw argument string)
        ;; into an argv-style array of dword pointers.  cc.py emits
        ;; `mov edi, ARGV; call FUNCTION_PARSE_ARGV` at program entry
        ;; whenever main takes argv, then reads ECX as argc — the same
        ;; shape as the 16-bit ABI, just widened to E-regs and a 4-byte
        ;; pointer stride.
        ;; Input:  EDI = buffer for argv pointers
        ;; Output: ECX = argc
        ;; Clobbers: EAX, ESI (and EDI advances past the populated slots)
        xor ecx, ecx
        mov esi, [EXEC_ARG]
        test esi, esi
        jz .parse_argv_done
        .parse_argv_scan:
        cmp byte [esi], ' '
        jne .parse_argv_check
        inc esi
        jmp .parse_argv_scan
        .parse_argv_check:
        cmp byte [esi], 0
        je .parse_argv_done
        mov [edi], esi
        add edi, 4
        inc ecx
        .parse_argv_end:
        cmp byte [esi], 0
        je .parse_argv_done
        cmp byte [esi], ' '
        je .parse_argv_term
        inc esi
        jmp .parse_argv_end
        .parse_argv_term:
        mov byte [esi], 0
        inc esi
        jmp .parse_argv_scan
        .parse_argv_done:
        ret
