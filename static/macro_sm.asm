        ;; macro_sm.asm — smoke test for the self-hosted assembler's
        ;; ``%macro`` / ``%endmacro`` support.  Exercises the two
        ;; shapes idt.asm uses: a data-emitting macro (IDT_ENTRY)
        ;; and a control-flow macro (EXC_NOERR) with label
        ;; concatenation via ``%1`` in a label definition.
        ;;
        ;; test_asm.py diffs asm.c's output against NASM's; byte
        ;; identity is the only contract.

        org 08048000h

%macro IDT_ENTRY 1
        dw %1
        dw 0x0008
        db 0
        db 0x8E
        dw 0
%endmacro

%macro EXC_NOERR 1
exc_%1:
        push word 0
        push word %1
        jmp exc_common
%endmacro

        ;; Invoke IDT_ENTRY with three operand shapes: a symbol
        ;; (``exc_common`` — resolves via the label table), a simple
        ;; hex literal, and the sum of a label and an integer.
        IDT_ENTRY exc_common
        IDT_ENTRY 0x1234
        IDT_ENTRY exc_common + 5

        EXC_NOERR 0
        EXC_NOERR 1
        EXC_NOERR 13

exc_common:
        jmp exc_common
