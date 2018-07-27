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
        call process_line
        jmp .prompt             ; Loop on user input

clear_screen:
        push ax
        mov ax, 03h
        int 10h
        mov dh, 0               ; Reset the row number
        pop ax
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

process_command:
        cld
        inc cx

        mov esi, buffer
        mov edi, command_clear
        repe cmpsb
        jz .clear

        mov esi, buffer
        mov edi, command_help
        repe cmpsb
        jz .help

        mov si, invalid_message
        jmp .done

        .clear:
        call clear_screen
        mov si, 0
        jmp .done

        .help:
        mov si, message_help
        .done:
        ret

process_line:
        call move_cursor_to_next_line
        cmp cx, 0               ; Test if command was typed
        jne .has_command
        mov si, zero_message
        jmp .output
        .has_command:
        call process_command
        .output:
        cmp si, 0
        jz .done
        call print_string
        call move_cursor_to_next_line
        .done:
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
        sub cx, buffer         ; Store how many characters were read in cx
        pop bx
        pop ax
        ret

        ;; Strings
        command_clear db `clear\0`
        command_help db `help\0`
        invalid_message db `invalid command\0`
        message_help db `Available commands: clear help\0`
        prompt db `$ \0`
        version db `Version 0.0.3dev\0`
        welcome db `Welcome to BBoeOS!\0`
        zero_message db `Nothing entered\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
