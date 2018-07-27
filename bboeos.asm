        org 7C00h               ; BIOS loads programs into 0x7C00 so we should

        %assign buffer 8C00h
                                ; set that as our program's origin
start:
        mov si, welcome
        call print_string
        call move_cursor_to_next_line
        mov si, version
        call print_string
        call move_cursor_to_next_line

        .prompt:
        mov si, prompt
        call print_string
        call read_line
        call handle_command
        jmp .prompt             ; Loop on user input

clear_screen:
        push ax
        mov ax, 0003h
        mov al, 03h
        int 10h
        mov dh, 0               ; Reset the row number
        pop ax
        ret

handle_command:
        call move_cursor_to_next_line
        cmp cx, 0               ; Test if command was typed
        jne .has_command
        mov si, zero_message
        jmp .done
        .has_command:
        mov si, buffer
        .done:
        call print_string
        call move_cursor_to_next_line
        ret

move_cursor_to_next_line:
        push ax
        push bx
        mov ah, 2               ; int 10h 'set cursor position' function
        mov bh, 0
        inc dh                  ; Move cursor to next row
        int 10h                 ; Call 'set cursor position' function
        pop bx
        pop ax
        ret

print_string:
        push ax
        push bx
        mov ah, 0Eh             ; int 10h 'print char' function
        mov bx, 0

        .repeat:
        lodsb                   ; Load the next character from the string
        cmp al, `\0`
        je .end                 ; If character is '\0', end the loop
        int 10h                 ; Call 'print char' function
        jmp .repeat
        .end:
        pop bx
        pop ax
        ret

read_line:
        push ax
        push bx
        mov cx, buffer

        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; 'Call 'keyboard read' function

        cmp al, `\r`            ; Loop until '\r' is read (return key)
        je .end
        cmp al, 0               ; Ignore special characters
        je .read_char
        mov ah, 0Eh             ; echo character
        mov bx, 0
        int 10h

        mov bx, cx              ; Add character to buffer
        mov byte [bx], al
        inc cx

        jmp .read_char

        .end:
        mov bx, cx              ; Add null terminating character to buffer
        mov byte [bx], 00h
        sub cx, buffer          ; Store how many characters were read in cx
        pop bx
        pop ax
        ret

        ;; Strings
        prompt db `$ \0`
        version db `Version 0.0.3dev\0`
        welcome db `Welcome to BBoeOS!\0`
        zero_message db `Nothing entered\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
