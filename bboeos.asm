        org 7C00h               ; BIOS loads programs into 0x7C00 so we should

        %assign buffer 8C00h
                                ; set that as our program's origin
start:
        mov cl, 0               ; Advance cursor after output

        mov si, welcome
        call print_string

        mov si, version
        call print_string

        .prompt:

        mov bx, buffer          ; Initialize character counter

        mov cl, 1               ; Don't advance cursor after output
        mov si, prompt
        call print_string

        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; 'Call 'keyboard read' function

        cmp al, `\r`            ; Loop until '\r' is read (return key)
        je .handle_command
        cmp al, 0               ; Ignore special characters
        je .read_char
        cmp al, 1Bh             ; Special command on escape
        je .clear_screen

        mov byte [bx], al
        .done_incrementing:
        inc bx                  ; Increment character counter

        mov ah, 0Eh             ; int 10h 'print char' function
        int 10h
        jmp .read_char

        .handle_command:
        mov byte [bx], 00h
        call handle_command
        call advance_cursor
        jmp .prompt             ; Loop on user input

        .clear_screen:
        mov ah, 00h
        mov al, 03h
        int 10h
        mov dh, 0
        jmp .prompt

advance_cursor:
        mov ah, 2               ; int 10h 'set cursor position' function
        mov bh, 0
        inc dh                  ; Move cursor to next row
        int 10h                 ; Call 'set cursor position' function
        ret

handle_command:
        call advance_cursor
        cmp bx, 0
        jne .has_command
        mov si, zero_message
        jmp .done
        .has_command:
        mov si, buffer
        .done:
        call print_string
        ret

print_string:                   ; Routine: output string in `si` to screen
        mov ah, 0Eh             ; int 10h 'print char' function

        .repeat:
        lodsb                   ; Load the next character from the string
        cmp al, `\0`
        je .break               ; If character is '\0', end the loop
        int 10h                 ; Call 'print char' function
        jmp .repeat

        .break:
        cmp cl, 1               ; Skip cursor advance if cl is 1
        je .end

        call advance_cursor

        .end:
        ret

        ;; Strings
        command_message db `Something\0`
        prompt db `$ \0`
        version db `Version 0.0.3dev\0`
        welcome db `Welcome to BBoeOS!\0`
        zero_message db `Nothing entered\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
