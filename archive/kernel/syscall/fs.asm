        ;; ------------------------------------------------------------
        ;; Filesystem syscalls.  cc.py loads args directly into the regs
        ;; the kernel vfs_* helpers expect (SI=path, DI=second-path,
        ;; AL=flags), so each case is just a call + jmp .iret_cf.
        ;;
        ;; fs_chmod / fs_rename / fs_unlink each guard the shell binary
        ;; against being modified, renamed out from under us, or deleted.
        ;; The check keys on the literal path "bin/shell" — aliases like
        ;; "./bin/shell" still slip through, same as the 16-bit version.
        ;; ------------------------------------------------------------

        .check_shell:
        ;; Returns ZF set if ESI points to the shell path (null-terminated).
        ;; Preserves ESI/EDI/ECX.  Local to the fs group — if a second
        ;; subsystem ever needs the shell path, promote .shell_name to a
        ;; non-local label and move both back up to syscall.asm.
        push esi
        push edi
        push ecx
        cld
        mov edi, .shell_name
        mov ecx, .shell_name_len
        repe cmpsb
        pop ecx
        pop edi
        pop esi
        ret

        .fs_chmod:
        ;; SI = path, AL = flags.
        call .check_shell
        jne .fs_chmod_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_do:
        call vfs_chmod
        jmp .iret_cf

        .fs_mkdir:
        ;; SI = name.  vfs_mkdir returns AX = new sector on success.
        call vfs_mkdir
        jmp .iret_cf

        .fs_rename:
        ;; SI = old path, DI = new path.  Guard the shell as the rename
        ;; source only — vfs_rename's own "destination exists" check
        ;; refuses an attempt to rename over bin/shell.
        call .check_shell
        jne .fs_rename_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_do:
        call vfs_rename
        jmp .iret_cf

        .fs_rmdir:
        ;; SI = path.
        call vfs_rmdir
        jmp .iret_cf

        .fs_unlink:
        ;; SI = path.
        call .check_shell
        jne .fs_unlink_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_unlink_do:
        call vfs_delete
        jmp .iret_cf

        .shell_name            db "bin/shell", 0
        .shell_name_len        equ $ - .shell_name
