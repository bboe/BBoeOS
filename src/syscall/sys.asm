        ;; ------------------------------------------------------------
        ;; Process control syscalls.
        ;;
        ;; sys_exec loads the program and `jmp`s — never returns through
        ;; the dispatch tail.  sys_exit teleports back to the kernel's
        ;; saved ESP (set by shell_reload / sys_exec before each
        ;; `jmp PROGRAM_BASE`) and re-enters shell_reload, which respawns
        ;; the shell from a clean state — same shape as the 16-bit
        ;; `mov sp, [shell_sp]; jmp shell_reload`.
        ;; ------------------------------------------------------------

        .sys_exec:
        ;; SI = filename.  On success control goes to PROGRAM_BASE and
        ;; never returns; on error CF is set and AX holds an ERROR_*
        ;; code per the original 16-bit contract.
        call vfs_find
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
        mov edi, PROGRAM_BASE
        call vfs_load
        jc .exec_not_found
        jmp program_enter

        .sys_exit:
        ;; Restore the kernel's saved ESP and re-enter shell_reload to
        ;; respawn the shell.  shell_reload (and sys_exec) snapshotted
        ;; ESP just before each `jmp PROGRAM_BASE`, so this discards the
        ;; syscall gate's iret frame, our pushad snapshot, and whatever
        ;; the program piled on top — none of which we're returning to.
        ;; Mirrors the 16-bit `mov sp, [shell_sp]; jmp shell_reload`.
        mov esp, [shell_esp]
        sti
        jmp shell_reload

        .sys_reboot:
        ;; Does not return.
        call reboot

        .sys_shutdown:
        ;; Returns only if the host ignores the shutdown port — surface
        ;; CF=1 so userspace can fall back.
        call shutdown
        stc
        jmp .iret_cf
