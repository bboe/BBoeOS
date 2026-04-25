        ;; ------------------------------------------------------------
        ;; Process control syscalls.
        ;;
        ;; sys_exec / sys_exit stub to ERROR_NOT_FOUND / halt at this
        ;; commit because the program_enter / shell_esp / shell_reload
        ;; machinery they need lives in entry.asm and won't land until
        ;; the boot-restructure commit.  sys_reboot / sys_shutdown work
        ;; immediately because they call directly into system.asm.
        ;; The 16-bit originals are preserved under `%if 0` next to
        ;; each stub for reference.
        ;; ------------------------------------------------------------

        .sys_exec:
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf

%if 0   ; 16-bit original — kept for reference until shell_reload returns
        .sys_exec:
        ;; Execute program: SI = filename
        ;; On error: CF set, AL = ERROR_NOT_FOUND or ERROR_NOT_EXECUTE
        call vfs_find           ; populates vfs_found_*
        jc .exec_not_found
        test byte [vfs_found_mode], FLAG_EXECUTE
        jnz .exec_load
        mov al, ERROR_NOT_EXECUTE
        stc
        jmp .iret_cf
        .exec_not_found:
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_load:
        ;; Save SP from before INT 30h.  Our frame has 16 bytes of
        ;; pusha save area plus the 6-byte iret frame, so the caller's
        ;; pre-INT-30h SP is current SP + 22.
        mov bp, sp
        add bp, 22
        mov [shell_sp], bp
        mov di, PROGRAM_BASE
        call vfs_load           ; DI=dest → CF
        jnc .exec_run
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_run:
        call fd_init
        call bss_setup
        jmp PROGRAM_BASE
%endif

        .sys_exit:
        ;; No shell_reload to teleport into yet — halt.
        cli
        hlt
        jmp $-1

%if 0   ; 16-bit original — kept for reference until shell_reload returns
        .sys_exit:
        ;; Restore stack and reload shell (skips WELCOME and one-time
        ;; boot inits — those run once from boot_shell).
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov sp, [shell_sp]
        jmp shell_reload
%endif

        .sys_reboot:
        ;; Does not return.
        call reboot

        .sys_shutdown:
        ;; Returns only if the host ignores the shutdown port — surface
        ;; CF=1 so userspace can fall back.
        call shutdown
        stc
        jmp .iret_cf
