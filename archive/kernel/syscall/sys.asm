        ;; ------------------------------------------------------------
        ;; Process control syscalls.
        ;;
        ;; sys_exec loads the program, snapshots BUFFER + EXEC_ARG from
        ;; the dying shell's PD (the new program inherits them), tears
        ;; down the shell's PD, and `jmp`s program_enter to build the
        ;; new program's PD — never returns through the dispatch tail.
        ;;
        ;; sys_exit teleports back to the kernel's saved ESP (snapshotted
        ;; by program_enter before each iretd), tears down the dying
        ;; program's PD, and re-enters shell_reload to respawn the
        ;; shell.  Mirrors the legacy 16-bit `mov sp, [shell_sp]; jmp
        ;; shell_reload`, with the per-program-PD bookkeeping spliced in.
        ;; ------------------------------------------------------------

        .sys_exec:
        ;; ESI = filename in the calling shell's user-virt.  Active PD
        ;; is the shell's; we can read user pages directly until the
        ;; switch-to-template + destroy below.
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
        mov edi, program_scratch
        call vfs_load
        jc .exec_not_found
        ;; Snapshot BUFFER (256 B at user-virt 0x500) and EXEC_ARG (4 B
        ;; at user-virt 0x4FC) from the shell's PD before destroy.  These
        ;; bytes carry the program name + args from shell to the new
        ;; program; per-program PDs would otherwise hand the new program
        ;; freshly zeroed pages.
        push esi
        push edi
        mov esi, BUFFER
        mov edi, buffer_snapshot
        mov ecx, MAX_INPUT / 4
        cld
        rep movsd
        mov eax, [EXEC_ARG]
        mov [exec_arg_snapshot], eax
        pop edi
        pop esi
        ;; Reset kernel ESP to the snapshot (= top of program_enter's
        ;; saved kernel stack) — discards the syscall gate's iret
        ;; frame and pushad.  We never return to user mode through
        ;; this syscall; the new program's iretd happens inside
        ;; program_enter.
        mov esp, [shell_esp]
        ;; Switch CR3 to kernel_pd_template, then destroy the dying
        ;; shell's PD.
        mov eax, cr3
        push eax
        mov eax, [kernel_pd_template_phys]
        mov cr3, eax
        pop eax
        call address_space_destroy
        sti
        jmp program_enter

        .sys_exit:
        ;; Tear down the dying program's PD, restore kernel ESP, and
        ;; re-enter shell_reload to respawn.  Same pattern as sys_exec
        ;; (CR3 → template, address_space_destroy, restore ESP), but
        ;; without the program-load step.
        mov eax, cr3
        push eax
        mov eax, [kernel_pd_template_phys]
        mov cr3, eax
        pop eax
        call address_space_destroy
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
