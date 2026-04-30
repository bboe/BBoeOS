fd_read_console:
        ;; Read one byte from keyboard ring buffer or COM1, store at [EDI].
        ;; Arrow keys arrive as pre-encoded ESC sequences from ps2_handle_scancode;
        ;; each byte is returned separately on successive calls.
        ;; Returns AX = 1 on success, 0 if CX=0, CF clear throughout.
        ;;
        ;; Line discipline: CR (0x0D) is translated to LF (0x0A) on input
        ;; so consumers see Unix-style line endings regardless of source.
        ;; The PS/2 Enter scancode (0x0D in the keymap) and serial
        ;; terminals (xterm, putty, etc., which send 0x0D for Enter)
        ;; converge on 0x0A here.  put_character on the output path
        ;; already translates LF → CRLF, so the symmetry is clean.
        push ebx
        push ecx
        push edx
        push edi
        mov ebx, ecx            ; EBX = bytes available in buffer
        test ebx, ebx
        jz .rcon_zero
        .rcon_poll:
        sti
        call ps2_getc
        test al, al
        jnz .rcon_got_char
        push edx
        mov dx, 3FDh            ; COM1 Line Status Register
        in al, dx
        pop edx
        test al, 01h            ; bit 0 = data ready
        jz .rcon_poll
        push edx
        mov dx, 3F8h            ; COM1 Data Register
        in al, dx
        pop edx
        .rcon_got_char:
        cmp al, 0Dh             ; CR → LF normalization
        jne .rcon_store
        mov al, 0Ah
        .rcon_store:
        stosb
        mov eax, 1
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
        .rcon_zero:
        xor eax, eax
        pop edi
        pop edx
        pop ecx
        pop ebx
        clc
        ret

fd_write_console:
        ;; Write CX bytes from user buffer to console via put_character
        push ebx
        push ecx
        push edx
        push esi
        mov esi, [fd_write_buffer]
        mov ebx, ecx            ; EBX = count
        xor edx, edx            ; EDX = bytes written
        test ebx, ebx
        jz .wcon_done
        .wcon_loop:
        lodsb
        call put_character
        inc edx
        cmp edx, ebx
        jb .wcon_loop
        .wcon_done:
        mov eax, edx
        pop esi
        pop edx
        pop ecx
        pop ebx
        clc
        ret
