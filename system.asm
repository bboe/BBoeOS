graphics:
        pusha

        mov ax, 0Dh
        int 10h                 ; change to 16-color graphics mode
        xor dx, dx

        .loop:
        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; 'Call 'keyboard read' function

        cmp al, 'a'
        je .cursor_left
        cmp al, 'd'
        je .cursor_right
        cmp al, 'j'
        je .background_backward
        cmp al, 'k'
        je .background_forward
        cmp al, 'q'             ; Loop until 'q' is read (return key)
        je .end
        cmp al, 's'
        je .cursor_down
        cmp al, 'w'
        je .cursor_up
        jmp .loop

        .background_backward:
        dec byte [bg_color]
        jmp .change_background
        .background_forward:
        inc byte [bg_color]
        .change_background:
        mov ax, 0B00h
        mov bh, 0
        mov byte bl, [bg_color]
        int 10h                 ; update background color=
        jmp .loop

        .cursor_down:
        cmp dh, 24
        jge .wrap_top
        inc dh
        jmp .move_cursor
        .wrap_top:
        mov dh, 0
        jmp .move_cursor

        .cursor_left:
        cmp dl, 0
        jle .wrap_right
        dec dl
        jmp .move_cursor
        .wrap_right:
        mov dl, 39
        jmp .move_cursor

        .cursor_right:
        cmp dl, 39
        jge .wrap_left
        inc dl
        jmp .move_cursor
        .wrap_left:
        mov dl, 0
        jmp .move_cursor

        .cursor_up:
        cmp dh, 0
        jle .wrap_bottom
        dec dh
        jmp .move_cursor
        .wrap_bottom:
        mov dh, 24

        .move_cursor:
        mov ax, 0200h
        mov bh, 0
        int 10h

        mov ax, 092Ah
        mov bx, 0003h
        mov cx, 1
        int 10h
        jmp .loop

        .end:
        popa
        call clear_screen
        ret

reboot:
        int 19h                 ; Bootstrap loader — re-reads and executes boot sector
        ret

shutdown:
        ;; Try QEMU ACPI shutdown (PIIX4 PM control port)
        mov dx, 0604h
        mov ax, 2000h
        out dx, ax

        ;; Try Bochs/old QEMU shutdown port
        mov dx, 0B004h
        mov ax, 2000h
        out dx, ax

        ;; If still running, shutdown is not supported
        ret
