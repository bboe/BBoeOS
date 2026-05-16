        ;; repe_cmpsb.asm — smoke test for the self-hosted assembler's
        ;; ``repe`` / ``repz`` / ``repne`` / ``repnz`` prefix aliases and
        ;; the ``cmpsb`` / ``cmpsw`` string-compare mnemonics that cc.py
        ;; emits for its ``memcmp`` builtin.  Before this support, the
        ;; mnemonics fell through to handle_unknown_word, got swallowed
        ;; as bogus label definitions, and shifted every subsequent
        ;; jump's displacement (uniq.asm regression).
        ;;
        ;; test_asm.py diffs asm.c's output against NASM's; byte
        ;; identity is the only contract.

        [bits 32]
        org 08048000h

main:
        mov ecx, 4
        test ecx, ecx
        jz .done
        cld
        repe cmpsb
        je .done
        repz cmpsb
        jne .done
        repne cmpsb
        repnz cmpsb
        cmpsw
        mov eax, 1
.done:
        ret
