;;; tests/idt_test.asm — standalone smoke test for idt.asm + stage1_5.asm.
;;;
;;; Stage 1 MBR: set up real-mode state, print "R", load stage 2 from
;;; sectors 2..N via BIOS INT 13h, install IDTR, switch to protected mode.
;;;
;;; Stage 2: in 32-bit protected mode, print "P", trigger divide-by-zero to invoke
;;; exception 0.  The IDT's exc_common handler should print "EXC00\r\n"
;;; and halt.
;;;
;;; Expected serial output:  R\nP\nEXC00\r\n
;;;
;;; Build with tests/test_idt.sh.

        org 7C00h

        COM1_DATA equ 3F8h
        COM1_LSR  equ 3FDh
        LSR_THRE  equ 20h
        STAGE2_SECTORS equ 16           ; plenty of room for idt.asm + stage1_5.asm

[bits 16]
start:
        cli
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov sp, 7C00h
        sti

        mov [boot_disk], dl

        ;; Load stage 2 starting at 0x7E00 (sector 2, head 0, cyl 0).
        mov ax, 0200h | STAGE2_SECTORS
        mov bx, 7E00h
        mov cx, 2                       ; cyl 0 sector 2
        xor dh, dh
        mov dl, [boot_disk]
        int 13h
        jc .boot_error

        mov al, 'R'
        call serial_putc_16
        mov al, 10
        call serial_putc_16

        call idt_install
        jmp enter_protected_mode

        .boot_error:
        mov al, '!'
        call serial_putc_16
        .hang:
        hlt
        jmp .hang

serial_putc_16:
        push ax
        push dx
        mov ah, al
        .wait:
        mov dx, COM1_LSR
        in al, dx
        test al, LSR_THRE
        jz .wait
        mov dx, COM1_DATA
        mov al, ah
        out dx, al
        pop dx
        pop ax
        ret

        boot_disk db 0

        times 510 - ($-$$) db 0
        dw 0AA55h

;;; ----- Stage 2 (loaded at 0x7E00) -----

[bits 32]
protected_mode_entry:
        mov al, 'P'
        call exc_putc                   ; provided by idt.asm
        mov al, 10
        call exc_putc

        ;; Trigger divide-by-zero (#DE, vector 0).  Expected: IDT routes
        ;; through exc_0 → exc_common, which prints "EXC00\r\n" and halts.
        xor edx, edx
        xor ecx, ecx
        mov eax, 1
        div ecx

        ;; Unreachable.
        .unreached:
        hlt
        jmp .unreached

[bits 16]
%include "stage1_5.asm"
%include "idt.asm"

        ;; Pad to fill the sectors stage-1 tries to load.
        times STAGE2_SECTORS * 512 - ($ - $$ - 512) db 0
