shared_die:
        ;; Write CX bytes from SI to stdout, then exit
        call shared_write_stdout
shared_exit:
        ;; Exit program (reload shell)
        mov ah, SYS_EXIT
        int 30h

shared_get_character:
        ;; Read one byte from stdin via read syscall
        ;; Returns: AL = byte read
        push bx
        push cx
        push di
        mov bx, STDIN
        mov di, SECTOR_BUFFER
        mov cx, 1
        mov ah, SYS_IO_READ
        int 30h
        pop di
        pop cx
        pop bx
        mov al, [SECTOR_BUFFER]
        ret

shared_parse_argv:
        ;; Split [EXEC_ARG] at spaces into an argv-style pointer array.
        ;; Input:  DI = buffer for argv pointers (caller-provided)
        ;; Output: CX = argc (number of arguments)
        ;; Clobbers: AX, SI
        xor cx, cx
        mov si, [EXEC_ARG]
        test si, si
        jz .parse_argv_done
        .parse_argv_scan:
        cmp byte [si], ' '
        jne .parse_argv_check
        inc si
        jmp .parse_argv_scan
        .parse_argv_check:
        cmp byte [si], 0
        je .parse_argv_done
        mov [di], si
        add di, 2
        inc cx
        .parse_argv_end:
        cmp byte [si], 0
        je .parse_argv_done
        cmp byte [si], ' '
        je .parse_argv_term
        inc si
        jmp .parse_argv_end
        .parse_argv_term:
        mov byte [si], 0
        inc si
        jmp .parse_argv_scan
        .parse_argv_done:
        ret
