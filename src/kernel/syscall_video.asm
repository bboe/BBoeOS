        .video_mode:
        ;; Set video mode: AL = mode; clears serial and screen.  For
        ;; VIDEO_MODE_TEXT_80x25 we reset natively (works after the pmode
        ;; transition too).  Graphics modes fall through to INT 10h and
        ;; remain BIOS-dependent — a future VESA driver replaces them.
        push ax
        mov al, `\r`
        call serial_character
        mov al, 0Ch             ; Form feed
        call serial_character
        pop ax
        cmp al, VIDEO_MODE_TEXT_80x25
        jne .video_mode_bios
        call vga_clear_screen
        jmp .iret_done
        .video_mode_bios:
        xor ah, ah              ; INT 10h AH=00h set mode (AL), clears screen
        int 10h
        jmp .iret_done
