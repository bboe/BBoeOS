start:
        mov ax, 07C0h           ; Set data segment to where we're loaded
        mov ds, ax

        mov si, welcome_string  ; Put string position into SI
        call print_string

        mov si, version_string
        call print_string

        mov si, another_string
        call print_string

        jmp $                   ; Jump here - infinite loop!

        welcome_string db 'Welcome to BBoeOS!', 0
        version_string db 'Version 0.0.2dev', 0
        another_string db '...', 0

print_string:                   ; Routine: output string in SI to screen
        mov ah, 0Eh             ; int 10h 'print char' function

        .repeat:
        lodsb                   ; Get character from string
        cmp al, 0
        je .done                ; If char is zero, end of string
        int 10h                 ; Otherwise, print it
        jmp .repeat

        .done:
        mov ah, 2               ; Set cursor position command
        inc dh                  ; Move cursor to next row
        int 10h                 ; Call command
        ret

        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
