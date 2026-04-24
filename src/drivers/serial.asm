;;; serial.asm — COM1 serial port driver
;;;
;;; serial_character: AL → COM1 (polled output)
;;; serial_getc:      COM1 → AL (non-blocking input; AL=0/ZF if empty; DEL→BS)

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

serial_getc:
        ;; Non-blocking COM1 read.  Returns AL = char, or AL=0 (ZF set) if empty.
        ;; Translates DEL (0x7F) to BS (0x08) for serial terminal compatibility.
        push edx
        mov dx, COM1_LSR
        in al, dx
        test al, 1              ; bit 0 = data ready
        jz .no_data
        mov dx, COM1_DATA
        in al, dx
        cmp al, 7Fh             ; DEL → backspace
        jne .done
        mov al, 08h
        jmp .done
        .no_data:
        xor al, al
        .done:
        pop edx
        ret
