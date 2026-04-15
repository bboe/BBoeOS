        org 0600h

%include "constants.asm"

main:
        mov ah, SYS_VIDEO_MODE
        mov al, VIDEO_MODE_EGA_320x200_16
        int 30h
        xor dx, dx
        mov byte [bg_color], 0

.loop:
        call FUNCTION_GET_CHARACTER

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
        jmp .change_background
        .background_forward:
        inc byte [bg_color]
        .change_background:
        mov ax, 0B00h
        mov bh, 0
        mov byte bl, [bg_color]
        int 10h                 ; Update background color
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
        mov ah, SYS_VIDEO_MODE
        mov al, VIDEO_MODE_TEXT_80x25
        int 30h
        jmp FUNCTION_EXIT

;; Variables
bg_color db 0
