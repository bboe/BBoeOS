        org 7C00h               ; BIOS loads programs into 0x7C00 so we should
                                ; set that as our program's origin
        %assign buffer 9000h

start:
        xor dx, dx
        mov ds, dx

        call clear_screen
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
        push bx
        mov ax, 07h
        int 10h                 ; Set text-mode

        mov ax, 02h
        mov bx, 0
        mov dx, 0
        int 10h                 ; Reset the cursor

        pop bx
        pop ax
        ret

graphics:
        push ax
        mov ax, 0Dh
        int 10h                 ; change to 16-color graphics mode
        call handle_graphics_mode
        call clear_screen
        pop ax
        ret

handle_graphics_mode:
        pusha

        mov dx, 0

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
        inc dh
        jmp .move_cursor
        .cursor_left:
        dec dl
        jmp .move_cursor
        .cursor_right:
        inc dl
        jmp .move_cursor
        .cursor_up:
        dec dh
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
        mov edi, command_graphics
        repe cmpsb
        jz .graphics

        mov esi, buffer
        mov edi, command_help
        repe cmpsb
        jz .help

        mov esi, buffer
        mov edi, command_time
        repe cmpsb
        jz .time

        mov si, invalid_message
        jmp .end

        .clear:
        call clear_screen
        mov si, 0
        jmp .end

        .graphics:
        call graphics
        mov si, 0
        jmp .end

        .help:
        mov si, message_help
        jmp .end

        .time:
        mov si, command_time

        .end:
        ret

process_line:
        call move_cursor_to_next_line
        cmp cx, 0               ; Test if command was typed
        jz .end
        .has_command:
        call process_command
        .output:
        cmp si, 0
        jz .end
        call print_string
        call move_cursor_to_next_line
        .end:
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

        ;; Values
        bg_color db 0

        ;; Strings
        command_clear db `clear\0`
        command_graphics db `graphics\0`
        command_help db `help\0`
        command_time db `time\0`
        invalid_message db `that's a invalid command\0`
        message_help db `Available commands: clear graphics help time\0`
        prompt db `$ \0`
        version db `Version 0.1.0 (2018/07/27)\0`
        welcome db `Welcome to BBoeOS!\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
