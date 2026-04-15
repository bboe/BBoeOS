        org 0600h

%include "constants.asm"

main:
        cld
        mov si, PROMPT
        mov cx, PROMPT_LENGTH
        call FUNCTION_WRITE_STDOUT

        call read_line
        test cx, cx
        jz main

        ;; Split command at first space
        mov si, BUFFER
        mov word [EXEC_ARG], 0
.find_space:
        lodsb
        cmp al, ' '
        je .found_space
        test al, al
        jnz .find_space
        jmp .split_done
.found_space:
        mov byte [si-1], 0     ; Null-terminate command name
        mov [EXEC_ARG], si     ; Point to argument
.split_done:
        ;; DX = command name length including null terminator
        mov dx, si
        sub dx, BUFFER

.dispatch:
        mov bx, cmd_table
.loop:
        mov di, [bx]
        test di, di
        jz .not_found

        mov cx, dx
        mov si, BUFFER
        repe cmpsb
        jne .next

        call word [bx+2]
        jmp .output

.next:
        add bx, 4
        jmp .loop

.not_found:
        ;; Try to execute as external program by literal name
        mov si, BUFFER
        mov ah, SYS_EXEC
        int 30h                 ; Does not return on success
        cmp al, ERROR_NOT_EXECUTE
        je .not_exec
        ;; Not found in root: retry inside bin/
        mov si, BUFFER
        mov di, exec_path + 4   ; just past "bin/"
        mov cx, DIRECTORY_NAME_LENGTH    ; name + null
        .copy_name:
        lodsb
        stosb
        test al, al
        jz .copy_done
        loop .copy_name
        .copy_done:
        mov byte [di], 0        ; ensure null-termination
        mov si, exec_path
        mov ah, SYS_EXEC
        int 30h                 ; Does not return on success
        cmp al, ERROR_NOT_EXECUTE
        je .not_exec
        mov si, INVALID_COMMAND
        jmp .output
        .not_exec:
        mov si, NOT_EXECUTABLE

.output:
        test si, si
        jz main
        mov di, si
        call FUNCTION_PRINT_STRING
        jmp main

;; Command handlers
;; Return: SI = string to print, or SI = 0 for no output

cmd_help:
        push bx
        mov si, HELP_PREFIX
        mov cx, HELP_PREFIX_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov bx, cmd_table
.help_loop:
        mov di, [bx]
        test di, di
        jz .help_end
        push bx
        call FUNCTION_PRINT_STRING
        mov al, ' '
        call FUNCTION_PRINT_CHARACTER
        pop bx
        add bx, 4
        jmp .help_loop
.help_end:
        pop bx
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        xor si, si
        ret

cmd_reboot:
        mov ah, SYS_REBOOT
        jmp syscall_null

cmd_shutdown:
        mov ah, SYS_SHUTDOWN
        int 30h
        mov si, SHUTDOWN_FAIL
        ret

;; Line editor

read_line:
        push ax
        push bx
        push dx
        mov cx, BUFFER          ; Cursor position
        mov dx, BUFFER          ; End of buffer

        .read_char:
        call FUNCTION_GET_CHARACTER

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
        cmp al, 7Fh             ; DEL (serial terminal backspace)
        je .backspace
        cmp al, 0Bh             ; Ctrl+K — kill to end of line
        je .ctrl_k
        cmp al, 0Ch             ; Ctrl+L — clear screen
        je .ctrl_l
        cmp al, `\r`            ; Enter
        je .end
        cmp al, 19h             ; Ctrl+Y — yank from kill buffer
        je .ctrl_y
        cmp al, 20h             ; Ignore other control characters
        jl .read_char

        call .insert_char       ; Insert character at cursor
        jnc .read_char
        call visual_bell
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
        cmp cx, BUFFER
        je .read_char
        dec cx
        mov bx, 1
        call emit_cursor_back
        jmp .read_char

        .cursor_right:
        cmp cx, dx
        je .read_char
        mov bx, cx
        mov al, [bx]
        call putc
        inc cx
        jmp .read_char

        .backspace:
        cmp cx, BUFFER
        je .read_char
        dec cx
        mov bx, 1
        call emit_cursor_back
        call .delete_at_cursor
        jmp .read_char

        .delete:
        cmp cx, dx
        je .read_char
        call .delete_at_cursor
        jmp .read_char

        .ctrl_a:
        cmp cx, BUFFER
        je .read_char
        mov bx, cx
        sub bx, BUFFER
        call emit_cursor_back
        mov cx, BUFFER
        jmp .read_char

        .ctrl_c:
        mov al, `\n`
        call putc
        mov cx, BUFFER
        mov dx, BUFFER
        jmp .return

        .ctrl_d:
        mov ah, SYS_SHUTDOWN
        int 30h
        jmp .read_char          ; If shutdown fails, continue

        .ctrl_e:
        cmp cx, dx
        je .read_char
        .ce_loop:
        mov bx, cx
        mov al, [bx]
        call putc
        inc cx
        cmp cx, dx
        jne .ce_loop
        jmp .read_char

        .ctrl_k:
        cmp cx, dx
        je .read_char
        push si
        push di
        ;; Copy killed text to kill buffer
        mov si, cx
        mov di, kill_buffer
        mov bx, dx
        sub bx, cx
        cmp bx, MAX_INPUT
        jbe .ck_save
        mov bx, MAX_INPUT
        .ck_save:
        mov [kill_length], bx
        .ck_copy:
        mov al, [si]
        mov [di], al
        inc si
        inc di
        dec bx
        jnz .ck_copy
        ;; Erase killed text: print spaces, then cursor back
        mov bx, dx
        sub bx, cx              ; Count of chars to erase
        push bx                 ; Save count for cursor_back
        mov si, bx
        .ck_erase:
        mov al, ' '
        call putc
        dec si
        jnz .ck_erase
        pop bx
        call emit_cursor_back
        mov dx, cx              ; Truncate buffer at cursor
        pop di
        pop si
        jmp .read_char

        .ctrl_y:
        push si
        mov si, kill_buffer
        mov bx, [kill_length]
        test bx, bx
        jz .cy_done
        .cy_loop:
        mov al, [si]
        push bx
        call .insert_char
        pop bx
        jc .cy_full             ; Stop yanking if buffer full
        inc si
        dec bx
        jnz .cy_loop
        jmp .cy_done
        .cy_full:
        call visual_bell
        .cy_done:
        pop si
        jmp .read_char

        .ctrl_l:
        mov ah, SYS_VIDEO_MODE
        mov al, VIDEO_MODE_TEXT_80x25
        int 30h
        mov cx, BUFFER
        mov dx, BUFFER
        jmp .return

        .end:
        mov al, `\n`
        call putc
        .return:
        mov bx, dx              ; Add null terminating character to buffer
        mov byte [bx], 00h
        mov cx, dx
        sub cx, BUFFER         ; Store how many characters were read in cx
        pop dx
        pop bx
        pop ax
        ret

        ;; Insert char in AL at cursor, shift buffer right, redraw
        .insert_char:
        push bx
        mov bx, dx
        sub bx, BUFFER
        cmp bx, MAX_INPUT
        pop bx
        jb .ic_ok
        stc                     ; Set carry flag to signal buffer full
        ret
        .ic_ok:
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
        ;; Print from cursor to end via putc
        mov si, cx
        .ic_print:
        cmp si, dx
        jge .ic_repos
        mov bx, si
        mov al, [bx]
        call putc
        inc si
        jmp .ic_print
        .ic_repos:
        inc cx
        mov bx, dx
        sub bx, cx
        call emit_cursor_back
        clc                     ; Clear carry flag to signal success
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
        ;; Print from cursor to end, space to erase, then cursor back
        mov si, cx
        .dac_print:
        cmp si, dx
        jge .dac_erase
        mov bx, si
        mov al, [bx]
        call putc
        inc si
        jmp .dac_print
        .dac_erase:
        mov al, ' '
        call putc               ; Erase trailing character
        mov bx, dx
        sub bx, cx
        inc bx
        call emit_cursor_back
        pop si
        ret

;; Utility functions

emit_cursor_back:
        ;; Emit ESC[nD sequence via putc
        ;; Input: BX = count (0 = no-op)
        test bx, bx
        jz .ecb_done
        push ax
        mov al, 1Bh
        call putc
        mov al, '['
        call putc
        mov ax, bx
        call .emit_decimal
        mov al, 'D'
        call putc
        pop ax
.ecb_done:
        ret

.emit_decimal:
        ;; Emit AX as decimal digits via putc
        push cx
        push dx
        xor cx, cx              ; Digit count
.ed_div:
        xor dx, dx
        mov bx, 10
        div bx                  ; AX = quotient, DX = remainder
        push dx                 ; Push digit
        inc cx
        test ax, ax
        jnz .ed_div
.ed_print:
        pop ax
        add al, '0'
        call putc
        loop .ed_print
        pop dx
        pop cx
        ret

putc:
        ;; Print char in AL via kernel jump table
        jmp FUNCTION_PRINT_CHARACTER

syscall_null:
        int 30h
        xor si, si
        ret

visual_bell:
        push ax
        push bx
        push cx
        push dx
        mov ax, 0B00h
        mov bx, 0004h          ; Border color = red
        int 10h
        mov ah, 86h
        xor cx, cx
        mov dx, 0C350h         ; 50,000 µs = 50ms
        int 15h
        mov ax, 0B00h
        xor bx, bx             ; Border color = black
        int 10h
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;; Command table
cmd_table:
        dw .help,     cmd_help
        dw .reboot,   cmd_reboot
        dw .shutdown, cmd_shutdown
        dw 0
        .help     db `help\0`
        .reboot   db `reboot\0`
        .shutdown db `shutdown\0`

;; Strings
HELP_PREFIX   db `Commands: `
HELP_PREFIX_LENGTH equ $ - HELP_PREFIX
INVALID_COMMAND   db `unknown command\n\0`
NOT_EXECUTABLE      db `not executable\n\0`
PROMPT        db `$ `
PROMPT_LENGTH equ $ - PROMPT
SHUTDOWN_FAIL db `APM shutdown failed\n\0`

;; Variables
exec_path     db `bin/`              ; 4 bytes prefix
              times DIRECTORY_NAME_LENGTH+1 db 0 ; name (up to 26 chars) + null + safety byte
kill_buffer   times MAX_INPUT db 0
kill_length   dw 0

