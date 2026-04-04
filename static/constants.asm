        ;; Simplified constants for assembler test
        %assign BUFFER 500h
        %assign DIR_ENTRY_SIZE 32
        %assign DIR_MAX_ENTRIES 32
        %assign DIR_SECTOR 10
        %assign DISK_BUFFER 9000h
        %assign EXEC_ARG 4FEh
        %assign FLAG_EXEC 01h
        %assign MAX_INPUT 256
        %assign PROGRAM_BASE 6000h

        %assign SYS_FS_CHMOD  00h
        %assign SYS_FS_COPY   01h
        %assign SYS_FS_CREATE 02h
        %assign SYS_FS_FIND   03h
        %assign SYS_FS_READ   04h
        %assign SYS_FS_RENAME 05h
        %assign SYS_FS_WRITE  06h

        %assign SYS_IO_GETC 10h
        %assign SYS_IO_PUTC 12h
        %assign SYS_IO_PUTS 13h

        %assign SYS_SCR_CLEAR 40h

        %assign SYS_EXEC 0F0h
        %assign SYS_EXIT 0F1h
        %assign SYS_REBOOT 0F2h
        %assign SYS_SHUTDOWN 0F3h
