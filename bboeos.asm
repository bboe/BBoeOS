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

        xor ax, ax
        int 13h                 ; reset disk
        jc .error

        call print_date
        mov al, ' '
        call print_char
        call print_time
        mov si, NEWLINE
        call print_string

        mov ax, 0202h           ; read 2 sectors
        mov bx, 7E00h           ; 0x7C00 + 512
        mov cx, 2               ; start at cylinder 0 sector 2
        mov dh, 0               ; start at head 0
        mov dl, [boot_disk]
        int 13h                 ; read

        jc .error
        cmp al, 2
        jne .error

        xor ah, ah
        int 1Ah                 ; CX:DX = ticks since midnight
        mov [boot_ticks_low], dx
        mov [boot_ticks_high], cx
        jmp cli

        .error:
        mov si, DISK_FAILURE
        call print_string

        .halt:
        hlt
        jmp .halt

clear_screen:
        push ax
        mov ax, 03h
        int 10h                 ; Set 80x25 color text mode
        pop ax
        ret

print_string:
        push ax
        push bx
        mov ah, 0Eh             ; int 10h 'print char' function
        xor bx, bx

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

        ;; Variables
        boot_disk db 0

        ;;  Constants
        DISK_FAILURE db `Disk failure\r\n\0`
        NEWLINE db `\r\n\0`
        WELCOME db `Welcome to BBoeOS!\r\nVersion 0.3.0 (2026/03/27)\r\n\0`

print_bcd:
        ;; Print AL as two BCD digits
        push ax
        push cx
        mov cl, al
        shr al, 4              ; High nibble
        add al, '0'
        call print_char
        mov al, cl
        and al, 0Fh            ; Low nibble
        add al, '0'
        call print_char
        pop cx
        pop ax
        ret

print_char:
        push ax
        push bx
        mov ah, 0Eh
        xor bx, bx
        int 10h
        pop bx
        pop ax
        ret

print_date:
        push ax
        push cx
        push dx
        mov ah, 04h
        int 1Ah
        mov al, ch              ; Century
        call print_bcd
        mov al, cl              ; Year
        call print_bcd
        mov al, '-'
        call print_char
        mov al, dh              ; Month
        call print_bcd
        mov al, '-'
        call print_char
        mov al, dl              ; Day
        call print_bcd
        pop dx
        pop cx
        pop ax
        ret

print_time:
        push ax
        push cx
        push dx
        mov ah, 02h
        int 1Ah
        mov al, ch              ; Hours
        call print_bcd
        mov al, ':'
        call print_char
        mov al, cl              ; Minutes
        call print_bcd
        mov al, ':'
        call print_char
        mov al, dh              ; Seconds
        call print_bcd
        pop dx
        pop cx
        pop ax
        ret

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature


cli:
        mov si, prompt
        call print_string
        call read_line
        test cx, cx
        jz cli
        call process_command
        jmp cli

graphics:
        push ax
        mov ax, 0Dh
        int 10h                 ; change to 16-color graphics mode
        call handle_graphics_mode
        call clear_screen
        pop ax
        ret

handle_clear:
        call clear_screen
        xor si, si
        ret

handle_date:
        call print_date
        mov si, newline
        ret

handle_graphics:
        call graphics
        xor si, si
        ret

handle_graphics_mode:
        pusha

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
        ret

handle_help:
        call print_help
        xor si, si
        ret

handle_reboot:
        call reboot
        xor si, si
        ret

handle_shutdown:
        call shutdown
        mov si, shutdown_fail
        ret

handle_time:
        call print_time
        mov si, newline
        ret

handle_uptime:
        call print_uptime
        mov si, newline
        ret

print_help:
        push bx
        mov si, help_prefix
        call print_string
        mov bx, command_table
        .loop:
        mov si, [bx]
        test si, si
        jz .end
        call print_string
        mov al, ' '
        call print_char
        add bx, 4
        jmp .loop
        .end:
        mov si, newline
        call print_string
        pop bx
        ret

print_dec_byte:
        ;; Print AL as 2 decimal digits
        push ax
        push cx
        xor ah, ah
        mov cl, 10
        div cl                  ; AL = tens, AH = ones
        add al, '0'
        call print_char
        mov al, ah
        add al, '0'
        call print_char
        pop cx
        pop ax
        ret

print_uptime:
        push eax
        push ecx
        push edx

        xor ah, ah
        int 1Ah                 ; CX:DX = current ticks since midnight

        movzx eax, cx           ; Build 32-bit current ticks in EAX
        shl eax, 16
        or ax, dx

        movzx ecx, word [boot_ticks_high]
        shl ecx, 16
        or cx, [boot_ticks_low]

        sub eax, ecx            ; EAX = elapsed ticks

        xor edx, edx
        mov ecx, 18
        div ecx                 ; EAX = elapsed seconds

        xor edx, edx
        mov ecx, 3600
        div ecx                 ; EAX = hours, EDX = remaining seconds

        push dx                 ; Save remaining seconds
        call print_dec_byte     ; Print hours (in AL)
        mov al, ':'
        call print_char

        pop ax                  ; Remaining seconds
        xor ah, ah
        mov cl, 60
        div cl                  ; AL = minutes, AH = seconds

        push ax                 ; Save seconds
        call print_dec_byte     ; Print minutes (in AL)
        mov al, ':'
        call print_char

        pop ax
        mov al, ah              ; Seconds
        call print_dec_byte

        pop edx
        pop ecx
        pop eax
        ret

process_command:
        push bx
        push dx
        cld
        inc cx
        mov dx, cx              ; Save string length in DX

        mov bx, command_table
        .loop:
        mov di, [bx]            ; Load command string pointer
        test di, di
        jz .invalid             ; End of table — no match

        mov cx, dx              ; Restore length
        mov si, buffer
        repe cmpsb
        jnz .next

        call word [bx+2]        ; Call handler
        jmp .end

        .next:
        add bx, 4
        jmp .loop

        .invalid:
        mov si, invalid_message

        .end:
        test si, si
        jz .done
        call print_string
        .done:
        pop dx
        pop bx
        ret

read_line:
        push ax
        push bx
        push dx
        mov cx, buffer          ; Cursor position
        mov dx, buffer          ; End of buffer

        .read_char:
        mov ah, 00h             ; int 16h 'keyboard read' function
        int 16h                 ; Call 'keyboard read' function

        cmp al, 0               ; Extended key
        je .extended_key
        cmp al, 0E0h            ; Extended key (alternate)
        je .extended_key
        cmp al, 01h             ; Ctrl+A — beginning of line
        je .ctrl_a
        cmp al, 02h             ; Ctrl+B — cursor left
        je .cursor_left
        cmp al, 03h             ; Ctrl+C — cancel line
        je .ctrl_c
        cmp al, 04h             ; Ctrl+D — shutdown
        je .ctrl_d
        cmp al, 05h             ; Ctrl+E — end of line
        je .ctrl_e
        cmp al, 06h             ; Ctrl+F — cursor right
        je .cursor_right
        cmp al, `\b`            ; Backspace
        je .backspace
        cmp al, 0Bh             ; Ctrl+K — kill to end of line
        je .ctrl_k
        cmp al, 0Ch             ; Ctrl+L — clear screen
        je .ctrl_l
        cmp al, `\r`            ; Enter
        je .end
        cmp al, 20h             ; Ignore other control characters
        jl .read_char

        call .insert_char       ; Insert character at cursor
        jmp .read_char

        .extended_key:
        cmp ah, 4Bh             ; Left arrow
        je .cursor_left
        cmp ah, 4Dh             ; Right arrow
        je .cursor_right
        cmp ah, 53h             ; Delete
        je .delete
        jmp .read_char          ; Ignore other extended keys

        .cursor_left:
        cmp cx, buffer
        je .read_char
        dec cx
        mov ah, 0Eh
        xor bx, bx
        mov al, `\b`
        int 10h
        jmp .read_char

        .cursor_right:
        cmp cx, dx
        je .read_char
        mov ah, 0Eh
        xor bx, bx
        mov bx, cx
        mov al, [bx]            ; Print char under cursor to advance
        int 10h
        inc cx
        jmp .read_char

        .backspace:
        cmp cx, buffer
        je .read_char
        dec cx
        mov ah, 0Eh
        xor bx, bx
        mov al, `\b`
        int 10h
        call .delete_at_cursor
        jmp .read_char

        .delete:
        cmp cx, dx
        je .read_char
        call .delete_at_cursor
        jmp .read_char

        .ctrl_a:
        cmp cx, buffer
        je .read_char
        mov ah, 0Eh
        xor bx, bx
        .ca_loop:
        mov al, `\b`
        int 10h
        dec cx
        cmp cx, buffer
        jne .ca_loop
        jmp .read_char

        .ctrl_c:
        mov ah, 0Eh
        xor bx, bx
        mov al, `\r`
        int 10h
        mov al, `\n`
        int 10h
        mov cx, buffer
        mov dx, buffer
        jmp .return

        .ctrl_d:
        call shutdown
        jmp .read_char          ; If shutdown fails, continue

        .ctrl_e:
        cmp cx, dx
        je .read_char
        mov ah, 0Eh
        .ce_loop:
        mov bx, cx
        mov al, [bx]
        xor bx, bx
        int 10h
        inc cx
        cmp cx, dx
        jne .ce_loop
        jmp .read_char

        .ctrl_k:
        cmp cx, dx
        je .read_char
        push si
        mov ah, 0Eh
        xor bx, bx
        mov si, dx
        sub si, cx              ; Count of chars to erase
        push si                 ; Save count
        .ck_erase:
        mov al, ' '
        int 10h
        dec si
        jnz .ck_erase
        pop si                  ; Restore count
        .ck_back:
        mov al, `\b`
        int 10h
        dec si
        jnz .ck_back
        mov dx, cx              ; Truncate buffer at cursor
        pop si
        jmp .read_char

        .ctrl_l:
        call clear_screen
        mov cx, buffer          ; Reset to start of buffer
        mov dx, buffer
        jmp .return

        .end:
        mov ah, 0Eh
        xor bx, bx
        mov al, `\r`
        int 10h
        mov al, `\n`
        int 10h
        .return:
        mov bx, dx              ; Add null terminating character to buffer
        mov byte [bx], 00h
        mov cx, dx
        sub cx, buffer         ; Store how many characters were read in cx
        pop dx
        pop bx
        pop ax
        ret

        ;; Insert char in AL at cursor, shift buffer right, redraw
        .insert_char:
        push si
        push ax
        mov si, dx
        .ic_shift:
        cmp si, cx
        jle .ic_insert
        dec si
        mov al, [si]
        mov [si+1], al
        jmp .ic_shift
        .ic_insert:
        pop ax
        mov bx, cx
        mov [bx], al
        inc dx
        ;; Print from cursor to end
        mov ah, 0Eh
        xor bx, bx
        mov si, cx
        .ic_print:
        cmp si, dx
        jge .ic_repos
        mov al, [si]
        int 10h
        inc si
        jmp .ic_print
        ;; Backspace to cursor + 1
        .ic_repos:
        inc cx
        mov si, dx
        sub si, cx
        .ic_back:
        test si, si
        jz .ic_done
        mov al, `\b`
        int 10h
        dec si
        jmp .ic_back
        .ic_done:
        pop si
        ret

        ;; Delete char at cursor, shift buffer left, redraw
        .delete_at_cursor:
        push si
        mov si, cx
        inc si
        .dac_shift:
        cmp si, dx
        jge .dac_redraw
        mov al, [si]
        dec si
        mov [si], al
        inc si
        inc si
        jmp .dac_shift
        .dac_redraw:
        dec dx
        ;; Print from cursor to end, space to erase, backspace to cursor
        mov ah, 0Eh
        xor bx, bx
        mov si, cx
        .dac_print:
        cmp si, dx
        jge .dac_erase
        mov al, [si]
        int 10h
        inc si
        jmp .dac_print
        .dac_erase:
        mov al, ' '
        int 10h                 ; Erase trailing character
        mov si, dx
        sub si, cx
        inc si
        .dac_back:
        test si, si
        jz .dac_done
        mov al, `\b`
        int 10h
        dec si
        jmp .dac_back
        .dac_done:
        pop si
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

        ;; Values
        bg_color db 0
        boot_ticks_high dw 0
        boot_ticks_low  dw 0

        ;; Data
        command_table:
            dw .clear,    handle_clear
            dw .date,     handle_date
            dw .graphics, handle_graphics
            dw .help,     handle_help
            dw .reboot,   handle_reboot
            dw .shutdown, handle_shutdown
            dw .time,     handle_time
            dw .uptime,   handle_uptime
            dw 0
            .clear    db `clear\0`
            .date     db `date\0`
            .graphics db `graphics\0`
            .help     db `help\0`
            .reboot   db `reboot\0`
            .shutdown db `shutdown\0`
            .time     db `time\0`
            .uptime   db `uptime\0`

        ;; Strings
        help_prefix db `Available commands: \0`
        invalid_message db `that's an invalid command\r\n\0`
        newline db `\r\n\0`
        prompt db `$ \0`
        shutdown_fail db `APM shutdown not supported\r\n\0`
