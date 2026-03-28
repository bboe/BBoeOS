        org 7C00h               ; offset where bios loads our first stage
        %assign buffer 500h
        %assign dir_entry_size 16
        %assign dir_max_entries 32
        %assign dir_sector 6
        %assign disk_buffer 9000h
        %assign max_input 256
        %assign stage2_sectors (dir_sector - 2)

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

        mov ax, 0200h | stage2_sectors
        mov bx, 7E00h           ; 0x7C00 + 512
        mov cx, 2               ; start at cylinder 0 sector 2
        mov dh, 0               ; start at head 0
        mov dl, [boot_disk]
        int 13h                 ; read

        jc .error
        cmp al, stage2_sectors
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

        .repeat:
        lodsb                   ; Load the next character from the string
        cmp al, `\0`
        je .end                 ; If character is '\0', end the loop
        call print_char
        jmp .repeat
        .end:
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
        push dx
        push ax                 ; Save char for serial
        mov ah, 0Eh
        xor bx, bx
        int 10h
        ;; Mirror to COM1
        mov dx, 3FDh           ; Line status register
        .serial_wait:
        in al, dx
        test al, 20h           ; Transmit holding register empty?
        jz .serial_wait
        pop ax                  ; Restore original char
        mov dx, 3F8h           ; COM1 data register
        out dx, al
        pop dx
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

serial_char:
        ;; Write AL to COM1 (preserves all registers)
        push ax
        push dx
        push ax                 ; Save char
        mov dx, 3FDh           ; Line status register
        .wait:
        in al, dx
        test al, 20h           ; Transmit holding register empty?
        jz .wait
        pop ax                  ; Restore char
        mov dx, 3F8h           ; COM1 data register
        out dx, al
        pop dx
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

%include "readline.asm"
%include "commands.asm"
%include "io.asm"
%include "system.asm"

        ;; Values
        bg_color db 0
        boot_ticks_high dw 0
        boot_ticks_low  dw 0
        kill_buffer times max_input db 0
        kill_length dw 0

        ;; Data
        command_table:
            dw .cat,      handle_cat
            dw .clear,    handle_clear
            dw .date,     handle_date
            dw .graphics, handle_graphics
            dw .help,     handle_help
            dw .ls,       handle_ls
            dw .reboot,   handle_reboot
            dw .shutdown, handle_shutdown
            dw .time,     handle_time
            dw .uptime,   handle_uptime
            dw 0
            .cat      db `cat\0`
            .clear    db `clear\0`
            .date     db `date\0`
            .graphics db `graphics\0`
            .help     db `help\0`
            .ls       db `ls\0`
            .reboot   db `reboot\0`
            .shutdown db `shutdown\0`
            .time     db `time\0`
            .uptime   db `uptime\0`

        ;; Strings
        cat_prefix db `cat \0`
        cat_usage db `Usage: cat <filename>\r\n\0`
        disk_error db `Disk read error\r\n\0`
        file_not_found db `File not found\r\n\0`
        help_prefix db `Available commands: \0`
        invalid_message db `that's an invalid command\r\n\0`
        newline db `\r\n\0`
        prompt db `$ \0`
        shutdown_fail db `APM shutdown not supported\r\n\0`
