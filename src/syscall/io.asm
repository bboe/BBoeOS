        ;; ------------------------------------------------------------
        ;; I/O syscalls.  cc.py loads args directly into the regs each
        ;; kernel fd_* helper expects (BX=fd, AL=flags/cmd, SI=buf for
        ;; write, DI=buf for read, CX=count), so cases are mostly bare
        ;; `call fd_*; jmp .iret_cf`.
        ;; ------------------------------------------------------------

        .io_close:
        ;; BX = fd.
        call fd_close
        jmp .iret_cf

        .io_fstat:
        ;; BX = fd.  fd_fstat returns AL = mode, CX:DX = size (32-bit).
        ;; The 16-bit dispatcher wrote CX/DX into the saved-regs slots so
        ;; the user got mode in AL and size in CX:DX after iret.  Mirror
        ;; that here — saved CX is at SAVED_EAX-4 (24), saved DX at
        ;; SAVED_EAX-8 (20).
        call fd_fstat
        jc .iret_cf
        mov [esp + SYSCALL_SAVED_EDX], dx
        mov [esp + SYSCALL_SAVED_EDX + 4], cx           ; SAVED_ECX = SAVED_EDX + 4
        jmp .iret_cf

        .io_ioctl:
        ;; BX = fd, AL = cmd, other regs per (fd_type, cmd).
        call fd_ioctl
        jmp .iret_cf

        .io_open:
        ;; SI = filename, AL = flags, DL = mode (when O_CREAT).
        call fd_open
        jmp .iret_cf

        .io_read:
        ;; BX = fd, EDI = buffer, ECX = count.  fd_read returns the full
        ;; 32-bit byte count in EAX (or -1 on error), so route through the
        ;; .iret_cf_eax path that skips the sign-extend.
        call fd_read
        jmp .iret_cf_eax

        .io_write:
        ;; BX = fd, ESI = buffer, ECX = count.  Same return shape as io_read.
        call fd_write
        jmp .iret_cf_eax
