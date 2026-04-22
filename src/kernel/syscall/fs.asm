        .fs_chmod:
        ;; Change a file's flags: SI = filename, AL = new flags value
        ;; Protect the shell from modification
        call .check_shell
        jne .fs_chmod_find
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_find:
        call vfs_chmod          ; SI=path, AL=mode → CF, AL=error code
        jmp .iret_cf


        .fs_mkdir:
        ;; Create subdirectory: SI = name
        ;; Returns AX = allocated sector on success, CF on error
        call vfs_mkdir          ; SI=name → AX=sector, CF, AL=error code
        jmp .iret_cf


        .fs_rename:
        ;; Rename/move file: SI = old name, DI = new name
        ;; Protect the shell from being renamed
        call .check_shell
        jne .fs_rename_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_do:
        call vfs_rename         ; SI=old, DI=new → CF, AL=error code
        jmp .iret_cf


        .fs_unlink:
        ;; Delete a file: SI = filename
        call .check_shell
        jne .fs_unlink_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_unlink_do:
        call vfs_delete         ; SI=path → CF, AL=error code
        jmp .iret_cf
