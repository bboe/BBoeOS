;;; tests/pmode_test.asm — standalone smoke test for pmode.asm.
;;;
;;; Boots as an MBR, prints "R\n" from 16-bit real mode, switches to 32-bit
;;; flat pmode, prints "P\n" from the 32-bit code path, then halts.
;;;
;;; Build with the paired shell script `tests/test_pmode.sh`.

        org 7C00h

        ;; COM1 port offsets. Duplicated here rather than pulled from the
        ;; kernel includes so this test stays self-contained.
        COM1_DATA equ 0x3F8
        COM1_LSR  equ 0x3FD
        LSR_THRE  equ 0x20

[bits 16]
start:
        cli
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov sp, 0x7C00          ; stack grows down from 0x7C00 — safely below us
        sti

        mov al, 'R'
        call serial_putc_16
        mov al, 10
        call serial_putc_16

        jmp enter_protected_mode

[bits 32]
protected_mode_entry:
        mov al, 'P'
        call serial_putc_32
        mov al, 10
        call serial_putc_32

.halt:
        hlt
        jmp .halt

%include "pmode.asm"

[bits 16]
serial_putc_16:
        ;; AL = character. Preserves AX, DX.
        push ax
        push dx
        mov ah, al
.wait16:
        mov dx, COM1_LSR
        in al, dx
        test al, LSR_THRE
        jz .wait16
        mov dx, COM1_DATA
        mov al, ah
        out dx, al
        pop dx
        pop ax
        ret

[bits 32]
serial_putc_32:
        ;; AL = character. Preserves EAX, EDX.
        push eax
        push edx
        mov ah, al
.wait32:
        mov dx, COM1_LSR
        in al, dx
        test al, LSR_THRE
        jz .wait32
        mov dx, COM1_DATA
        mov al, ah
        out dx, al
        pop edx
        pop eax
        ret

        times 510-($-$$) db 0
        dw 0AA55h
