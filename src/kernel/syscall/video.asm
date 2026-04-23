        .video_mode:
        ;; Set video mode: AL = mode; clears serial and screen.
        ;; Uses native VGA register tables (vga_set_mode); unsupported modes are ignored.
        push ax
        mov al, `\r`
        call serial_character
        mov al, 0Ch             ; Form feed
        call serial_character
        pop ax
        call vga_set_mode
        jc .iret_done           ; unsupported mode — skip
        cmp al, VIDEO_MODE_TEXT_80x25
        jne .iret_done
        call vga_clear_screen   ; restore blank text buffer after text-mode reset
        jmp .iret_done
