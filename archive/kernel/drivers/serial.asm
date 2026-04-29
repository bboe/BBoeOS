;;; serial.asm — COM1 serial port driver
;;;
;;; serial_character: AL → COM1 (polled output)

        COM1_DATA               equ 3F8h
        COM1_LSR                equ 3FDh

serial_character:
        ;; Write AL to COM1.  Polls LSR.THRE first.  Preserves EAX, EDX.
        push eax
        push edx
        push eax
        mov dx, COM1_LSR
        .wait:
        in al, dx
        test al, 20h            ; THRE
        jz .wait
        pop eax
        mov dx, COM1_DATA
        out dx, al
        pop edx
        pop eax
        ret
