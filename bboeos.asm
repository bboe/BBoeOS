        org 7C00h               ; offset where bios loads our first stage
        %assign buffer 500h

start:
        ;; Set initial state
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov [boot_disk], dl

        mov ax, 50h             ; Linear 0x500 is start of free space
        cli                     ; Disable interrupts while adjusting stack
        mov ss, ax
        mov sp, 7700h           ; 0050h:7700h is equivalent to 0x7c00
        sti                     ; Enable interrupts

        call clear_screen
        mov si, WELCOME
        call print_string

        mov ax, 0
        int 13h                 ; reset disk
        jc .error

        mov si, LOADING
        call print_string

        mov ax, 0201h           ; read 1 sector
        mov bx, 7E00h           ; 0x7C00 + 512
        mov cx, 2               ; start at cylinder 0 sector 2
        mov dh, 0               ; start at head 0
        mov dl, [boot_disk]
        int 13h                 ; read

        jc .error
        cmp al, 1
        jne .error
        jmp cli

        .error:
        mov si, DISK_FAILURE
        call print_string

        .halt:
        hlt
        jmp .halt

clear_screen:
        push ax
        push bx
        push dx
        mov ax, 07h
        int 10h                 ; Set text-mode

        mov ax, 02h
        mov bx, 0
        mov dx, 0
        int 10h                 ; Reset the cursor

        pop dx
        pop bx
        pop ax
        ret

print_string:
        push ax
        push bx
        push dx
        mov ah, 0Eh             ; int 10h 'print char' function
        mov bx, 0

        .repeat:
        lodsb                   ; Load the next character from the string
        cmp al, `\0`
        je .end                 ; If character is '\0', end the loop
        int 10h                 ; Call 'print char' function
        jmp .repeat
        .end:
        pop dx
        pop bx
        pop ax
        ret

        ;; Variables
        boot_disk db 0

        ;;  Constants
        DISK_FAILURE db `Disk failure\r\n\0`
        LOADING db `Loading...\r\n\0`
        WELCOME db `Welcome to BBoeOS!\r\nVersion 0.2.0 (2018/08/11)\r\n\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature


cli:
        mov si, prompt
        call print_string
        call read_line
        call process_line
        jmp cli             ; Loop on user input


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

process_command:
        ;; Input string length is in cx
        push bx

        cld
        inc cx
        mov bx, cx              ; Save string length

        mov si, buffer
        mov di, command_clear
        repe cmpsb
        jz .clear

        mov cx, bx              ; Reset string length
        mov si, buffer
        mov di, command_graphics
        repe cmpsb
        jz .graphics

        mov cx, bx              ; Reset string length
        mov si, buffer
        mov di, command_help
        repe cmpsb
        jz .help

        mov cx, bx              ; Reset string length
        mov si, buffer
        mov di, command_time
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
        pop bx
        ret

process_line:
        cmp cx, 0               ; Test if command was typed
        jz .end
        .has_command:
        call process_command
        .output:
        cmp si, 0
        jz .end
        call print_string
        .end:
        ret

read_line:
        push ax
        push bx
        mov cx, buffer

        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; 'Call 'keyboard read' function

        cmp al, 0               ; Ignore special characters
        je .read_char
        cmp al, `\b`            ; Was backspace typed?
        je .backspace

        mov ah, 0Eh             ; echo character
        mov bx, 0
        int 10h

        cmp al, `\r`            ; Loop until '\r' is read (return key)
        je .end

        mov bx, cx              ; Add character to buffer
        mov byte [bx], al
        inc cx

        jmp .read_char

        .backspace:
        cmp cx, buffer
        je .read_char

        mov ah, 0Eh             ; echo character
        mov bx, 0
        int 10h

        mov al, ' '
        int 10h                 ; Output space character
        mov al, `\b`
        int 10h                 ; Output backspace character
        dec cx
        jmp .read_char

        .end:
        mov al, `\n`
        int 10h                 ; Output newline character
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
        invalid_message db `that's a invalid command\r\n\0`
        message_help db `Available commands: clear graphics help time\r\n\0`
        prompt db `$ \0`
