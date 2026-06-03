        ;; imul_three_operand.asm — smoke test for the two- and
        ;; three-operand imul-by-constant forms in the self-hosted
        ;; assembler.  cc.py emits the explicit three-operand shape
        ;; ``imul dst, src, imm`` to scale array indices (e.g.
        ;; ``imul eax, eax, 38``); NASM accepts both forms and the
        ;; assembler must encode each byte-identically.
        ;;
        ;; Both forms compile to the three-operand machine encoding
        ;; (``6B /r ib`` for an imm8, ``69 /r iw`` for an imm16) with
        ;; the modrm reg field holding the destination and the rm field
        ;; holding the source; the two-operand form defaults src=dst.

        [bits 32]
        org 08048000h

main:
        ;; ----- two-operand form (src defaults to dst) -----
        imul eax, 38            ; imm8
        imul ebx, 300           ; imm16
        imul ecx, -5            ; negative imm8

        ;; ----- three-operand, dst == src (the common cc.py case) -----
        imul eax, eax, 38       ; imm8, modrm == two-operand form

        ;; ----- three-operand, dst != src -----
        imul edx, eax, 5        ; imm8
        imul ebx, ecx, 300      ; imm16

        ret
