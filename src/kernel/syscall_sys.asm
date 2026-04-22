        .sys_exec:
        ;; Execute program: SI = filename
        ;; Saves shell stack, loads program at PROGRAM_BASE, jumps to it
        ;; On error: CF set, AL = ERROR_NOT_FOUND or ERROR_NOT_EXECUTE
        call find_file
        jc .exec_not_found
        jmp .exec_check_flag
        .exec_not_found:
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_check_flag:
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_EXECUTE
        jnz .exec_load
        mov al, ERROR_NOT_EXECUTE
        stc
        jmp .iret_cf
        .exec_load:
        ;; Save SP from before INT 30h.  Our frame has 16 bytes of
        ;; pusha save area plus the 6-byte iret frame, so the caller's
        ;; pre-INT-30h SP is current SP + 22.
        mov bp, sp
        add bp, 22
        mov [shell_sp], bp
        ;; Load program into PROGRAM_BASE
        mov di, PROGRAM_BASE
        call load_file
        jnc .exec_run
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_run:
        call fd_init
        jmp PROGRAM_BASE

        .sys_exit:
        ;; Restore stack and reload shell
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov sp, [shell_sp]
        jmp boot_shell

        .sys_reboot:
        call reboot
        iret

        .sys_shutdown:
        call shutdown
        iret
