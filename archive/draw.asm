        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_VIDEO_MODE
        mov al, VIDEO_MODE_EGA_320x200_16
        int 30h

        ;; Set foreground cyan (3) and background black (0) via SGR
        mov si, INIT_COLOR
        mov cx, INIT_COLOR_LEN
        call FUNCTION_WRITE_STDOUT

        xor dx, dx              ; DH = row, DL = col
        mov byte [bg_color], 0

.loop:
        ;; read(STDIN, input_buffer, 1)
        mov bx, STDIN
        mov di, input_buffer
        mov cx, 1
        mov ah, SYS_IO_READ
        int 30h
        mov al, [input_buffer]

        cmp al, 'a'
        je .cursor_left
        cmp al, 'd'
        je .cursor_right
        cmp al, 'j'
        je .background_backward
        cmp al, 'k'
        je .background_forward
        cmp al, 'q'
        je .end
        cmp al, 's'
        je .cursor_down
        cmp al, 'w'
        je .cursor_up
        jmp .loop

        .background_backward:
        dec byte [bg_color]
        jmp .emit_background
        .background_forward:
        inc byte [bg_color]
        .emit_background:
        ;; Emit "ESC[48;5;<bg>m"
        mov si, BG_PREFIX
        mov cx, BG_PREFIX_LEN
        call FUNCTION_WRITE_STDOUT
        mov al, [bg_color]
        call FUNCTION_PRINT_BYTE_DECIMAL
        mov al, 'm'
        call FUNCTION_PRINT_CHARACTER
        jmp .loop

        .cursor_down:
        cmp dh, 24
        jge .wrap_top
        inc dh
        jmp .emit_cursor
        .wrap_top:
        mov dh, 0
        jmp .emit_cursor

        .cursor_left:
        cmp dl, 0
        jle .wrap_right
        dec dl
        jmp .emit_cursor
        .wrap_right:
        mov dl, 39
        jmp .emit_cursor

        .cursor_right:
        cmp dl, 39
        jge .wrap_left
        inc dl
        jmp .emit_cursor
        .wrap_left:
        mov dl, 0
        jmp .emit_cursor

        .cursor_up:
        cmp dh, 0
        jle .wrap_bottom
        dec dh
        jmp .emit_cursor
        .wrap_bottom:
        mov dh, 24

        .emit_cursor:
        ;; Emit "ESC[<row+1>;<col+1>H*"
        push dx                 ; save row/col across calls
        mov si, ESC_CSI
        mov cx, 2
        call FUNCTION_WRITE_STDOUT
        pop dx
        push dx
        mov al, dh
        inc al
        call FUNCTION_PRINT_BYTE_DECIMAL
        mov al, ';'
        call FUNCTION_PRINT_CHARACTER
        pop dx
        push dx
        mov al, dl
        inc al
        call FUNCTION_PRINT_BYTE_DECIMAL
        mov si, CURSOR_SUFFIX
        mov cx, 2
        call FUNCTION_WRITE_STDOUT
        pop dx
        jmp .loop

        .end:
        mov ah, SYS_VIDEO_MODE
        mov al, VIDEO_MODE_TEXT_80x25
        int 30h
        jmp FUNCTION_EXIT

;; Strings
BG_PREFIX       db `\e[48;5;`
BG_PREFIX_LEN   equ $ - BG_PREFIX
CURSOR_SUFFIX   db `H*`
ESC_CSI         db `\e[`
INIT_COLOR      db `\e[38;5;3m\e[48;5;0m`
INIT_COLOR_LEN  equ $ - INIT_COLOR

;; Variables
bg_color        db 0
input_buffer    db 0
