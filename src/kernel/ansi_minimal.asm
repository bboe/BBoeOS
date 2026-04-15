        ;; Minimal output routines for stage 1 MBR
        ;; put_string: print null-terminated string at DS:SI
        ;; put_character_raw: write AL to screen (teletype) and serial,
        ;;                    converting \n to \r\n.  No ANSI parsing.
        ;; serial_character:  write AL to COM1

put_character_raw:
        push ax
        push bx
        ;; Convert \n to \r\n
        cmp al, 0Ah
        jne .emit
        push ax
        mov al, 0Dh
        call serial_character
        mov ah, 0Eh
        xor bx, bx
        int 10h
        pop ax
.emit:
        call serial_character
        mov ah, 0Eh
        xor bx, bx
        int 10h
        pop bx
        pop ax
        ret

put_string:
        push ax
.repeat:
        lodsb
        cmp al, 0
        je .end
        call put_character_raw
        jmp .repeat
.end:
        pop ax
        ret

serial_character:
        ;; Write AL to COM1 (preserves all registers)
        push ax
        push dx
        push ax
        mov dx, 3FDh
.wait:
        in al, dx
        test al, 20h
        jz .wait
        pop ax
        mov dx, 3F8h
        out dx, al
        pop dx
        pop ax
        ret
