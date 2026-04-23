        .io_close:
        ;; Close fd: BX = fd
        ;; CF on error
        call fd_close
        jmp .iret_cf

        .io_fstat:
        ;; Get file status: BX = fd
        ;; Returns AL = mode (permission flags), CX:DX = size (32-bit)
        ;; CF on error
        call fd_fstat
        mov [bp+12], cx         ; high 16 of size → saved CX
        mov [bp+10], dx         ; low 16 of size → saved DX
        jmp .iret_cf

        .io_ioctl:
        ;; Device control: BX = fd, AL = cmd, other regs per (type,cmd)
        ;; Returns CF set on error (invalid fd, unsupported type, or bad cmd)
        call fd_ioctl
        jmp .iret_cf

        .io_open:
        ;; Open file/device: SI = filename, AL = flags
        ;; Returns AX = fd, CF on error
        call fd_open
        jmp .iret_cf

        .io_read:
        ;; Read from fd: BX = fd, DI = buffer, CX = count
        ;; Returns AX = bytes read (0 = EOF), CF on error
        call fd_read
        jmp .iret_cf

        .io_write:
        ;; Write to fd: BX = fd, SI = buffer, CX = count
        ;; Returns AX = bytes written, or -1 on error
        call fd_write
        jmp .iret_cf
