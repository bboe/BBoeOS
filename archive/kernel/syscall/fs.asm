        ;; ------------------------------------------------------------
        ;; Filesystem syscalls.  cc.py loads args directly into the regs
        ;; the kernel vfs_* helpers expect (ESI=path, EDI=second-path,
        ;; AL=flags), so each case validates its user pointer(s) via
        ;; access_ok_string and then calls + jmp .iret_cf.
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

        .check_path:
        ;; Validates ESI as a null-terminated user path within MAX_PATH
        ;; bytes.  On failure: sets AL = ERROR_FAULT, sets CF, and jumps
        ;; out via .fs_bad_pointer (handlers fall through .iret_cf).
        ;; On success: returns with CF clear, registers preserved.
        push ecx
        mov ecx, MAX_PATH
        call access_ok_string
        pop ecx
        ret

        .fs_chmod:
        ;; ESI = path, AL = flags.
        call .check_path
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_chmod_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_do:
        call vfs_chmod
        jmp .iret_cf

        .fs_mkdir:
        ;; ESI = name.  vfs_mkdir returns AX = new sector on success.
        call .check_path
        jc .fs_bad_pointer
        call vfs_mkdir
        jmp .iret_cf

        .fs_rename:
        ;; ESI = old path, EDI = new path.  Guard the shell as the rename
        ;; source only — vfs_rename's own "destination exists" check
        ;; refuses an attempt to rename over bin/shell.
        call .check_path
        jc .fs_bad_pointer
        xchg esi, edi
        call .check_path
        xchg esi, edi
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_rename_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_do:
        call vfs_rename
        jmp .iret_cf

        .fs_rmdir:
        ;; ESI = path.
        call .check_path
        jc .fs_bad_pointer
        call vfs_rmdir
        jmp .iret_cf

        .fs_unlink:
        ;; ESI = path.
        call .check_path
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_unlink_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_unlink_do:
        call vfs_delete
        jmp .iret_cf

        .fs_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        .shell_name            db "bin/shell", 0
        .shell_name_len        equ $ - .shell_name
