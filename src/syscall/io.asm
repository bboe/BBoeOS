        ;; ------------------------------------------------------------
        ;; I/O syscalls.  cc.py loads args directly into the regs each
        ;; kernel fd_* helper expects (BX=fd, AL=flags/cmd, ESI=buf for
        ;; write, EDI=buf for read, ECX=count), so most cases are bare
        ;; `call fd_*; jmp .iret_cf`.
        ;;
        ;; Handlers that take a user pointer + length call access_ok
        ;; (or access_ok_string for null-terminated paths) before
        ;; dispatching to fd_*.  A bad pointer surfaces as CF=1 with
        ;; AL = ERROR_FAULT so the user sees an errno-style failure
        ;; rather than the kernel ever dereferencing the pointer.  The
        ;; CPL=0 #PF kill path in idt.asm catches the residual case
        ;; where access_ok passes but the user page is unmapped (e.g.
        ;; edit's gap buffer at virt 0x100000 before fault-in).
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
        ;; ESI = filename, AL = flags, DL = mode (when O_CREAT).
        push ecx
        mov ecx, MAX_PATH
        call access_ok_string
        pop ecx
        jc .io_open_bad_pointer
        call fd_open
        jmp .iret_cf
        .io_open_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        .io_read:
        ;; BX = fd, EDI = buffer, ECX = count.  fd_read returns the full
        ;; 32-bit byte count in EAX (or -1 on error), so route through the
        ;; .iret_cf_eax path that skips the sign-extend.  Bad-buffer is
        ;; surfaced the same way fd_read surfaces a closed-fd error:
        ;; EAX=-1 + CF=1, no errno encoding.
        push ebx
        mov ebx, edi
        call access_ok
        pop ebx
        jc .io_read_bad_pointer
        call fd_read
        jmp .iret_cf_eax
        .io_read_bad_pointer:
        or eax, -1
        stc
        jmp .iret_cf_eax

        .io_write:
        ;; BX = fd, ESI = buffer, ECX = count.  Same return shape as io_read.
        push ebx
        mov ebx, esi
        call access_ok
        pop ebx
        jc .io_write_bad_pointer
        call fd_write
        jmp .iret_cf_eax
        .io_write_bad_pointer:
        or eax, -1
        stc
        jmp .iret_cf_eax
