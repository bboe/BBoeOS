        org 7C00h               ; BIOS loads programs into 0x7C00 so we should
                                ; set that as our program's origin
start:
        mov si, welcome_string  ; Put string position into SI
        call print_string

        mov si, version_string
        call print_string

        mov si, another_string
        call print_string

        jmp $                   ; Jump here - infinite loop!

        welcome_string db `Welcome to BBoeOS!\0`
        version_string db `Version 0.0.2dev\0`
        another_string db `...\0`

print_string:                   ; Routine: output string in `si` to screen
        mov ah, 0Eh             ; int 10h 'print char' function

        .repeat:
        lodsb                   ; Load the next character from the string
        cmp al, 0
        je .done                ; If character is '\0', end the loop
        int 10h                 ; Call 'print char' function
        jmp .repeat

        .done:
        mov ah, 2               ; int 10h 'set cursor position' function
        inc dh                  ; Move cursor to next row
        int 10h                 ; Call 'set cursor position' function
        ret

        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature
