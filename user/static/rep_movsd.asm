        ;; rep_movsd.asm — smoke test for the self-hosted assembler's
        ;; ``movsd`` / ``stosd`` dword string mnemonics (and ``rep``
        ;; over them) that cc.py emits for 4-byte element fill/copy
        ;; loops.  test_asm.py diffs asm.c's output against NASM's;
        ;; byte identity is the only contract.

        [bits 32]
        org 08048000h

main:
        mov ecx, 4
        cld
        rep movsd
        rep stosd
        movsd
        stosd
        movsb
        stosb
        movsw
        stosw
        ret
