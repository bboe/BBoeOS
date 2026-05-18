        ;; times_branch_sm.asm — smoke test for ``times N <branch>``.
        ;;
        ;; Regression: the self-hosted assembler in src/c/asm.c only
        ;; handled ``times N db ...`` and silently elided every other
        ;; payload (``times N jmp X``, ``times N jcc X``, ``times N
        ;; <mnemonic>``) — emitting zero bytes while reporting OK.
        ;; NASM emits N copies of the instruction.  test_asm.py diffs
        ;; the in-OS asm's output against NASM's, so this file fails
        ;; loudly when the regression returns.

        org 08048000h

start:
        times 5 jmp start       ; 5 short 2-byte jmp = 10 bytes
        times 5 je  start       ; 5 short 2-byte je  = 10 bytes
        times 5 jne start       ; 5 short 2-byte jne = 10 bytes
end:
        ret
