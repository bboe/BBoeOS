        %assign BUFFER 500h
        %assign DIR_ENTRY_SIZE 32
        %assign DIR_MAX_ENTRIES 48
        %assign DIR_NAME_LEN 25         ; 24 chars + null
        %assign DIR_OFF_FLAGS (DIR_NAME_LEN)
        %assign DIR_OFF_SECTOR (DIR_NAME_LEN + 1)
        %assign DIR_OFF_SIZE (DIR_NAME_LEN + 3)   ; 32-bit (4 bytes)
        %assign DIR_SECTOR 13
        %assign DIR_SECTORS 3
        %assign DISK_BUFFER 0E000h    ; 512 bytes (one sector)
        %assign ERR_DIR_FULL  01h     ; Copy error: no free directory entries
        %assign ERR_EXISTS    02h     ; Rename/copy error: destination name already exists
        %assign ERR_NOT_EXEC  03h     ; Exec error: file exists but is not executable
        %assign ERR_NOT_FOUND 04h     ; File not found
        %assign ERR_PROTECTED 05h     ; Rename/chmod error: file is protected
        %assign EXEC_ARG 4FEh
        %assign FD_ENTRY_SIZE 32
        %assign FD_MAX 8
        %assign FD_OFF_DIR_OFF 14    ; offset of dir_off field within FD entry
        %assign FD_OFF_DIR_SEC 12    ; offset of dir_sec field within FD entry
        %assign FD_OFF_FLAGS 1       ; offset of flags field within FD entry
        %assign FD_OFF_MODE 16       ; offset of mode field (file permission flags)
        %assign FD_OFF_POS 8         ; offset of pos field within FD entry (32-bit)
        %assign FD_OFF_SIZE 4        ; offset of size field within FD entry (32-bit)
        %assign FD_OFF_START 2       ; offset of start_sec field within FD entry
        %assign FD_OFF_TYPE 0        ; offset of type field within FD entry
        %assign FD_TYPE_CONSOLE 2
        %assign FD_TYPE_DIR 3
        %assign FD_TYPE_FILE 1
        %assign FD_TYPE_FREE 0
        %assign FLAG_DIR  02h         ; Directory entry flags: bit 1 = subdirectory
        %assign FLAG_EXEC 01h         ; Directory entry flags: bit 0 = executable
        %assign MAX_INPUT 256
        %assign NE2K_BASE 300h
        %assign NET_RX_BUF 0E800h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign NET_TX_BUF 0E200h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign O_CREAT  10h
        %assign O_RDONLY 00h
        %assign O_TRUNC  20h
        %assign O_WRONLY 01h
        %assign PROGRAM_BASE 0600h
        %assign STDERR 2
        %assign STDIN 0
        %assign STDOUT 1

        ;; Syscall numbers (INT 30h, passed in AH)
        %assign SYS_FS_CHMOD  00h
        %assign SYS_FS_MKDIR  01h
        %assign SYS_FS_RENAME 02h

        %assign SYS_IO_CLOSE 10h    ; BX=fd; CF on error
        %assign SYS_IO_FSTAT 11h    ; BX=fd; returns AL=mode, CX:DX=size (32-bit), CF on error
        %assign SYS_IO_GETC  12h
        %assign SYS_IO_OPEN  13h    ; SI=filename, AL=flags, DL=mode; returns AX=fd, CF on error
        %assign SYS_IO_PUTC  14h
        %assign SYS_IO_PUTS  15h
        %assign SYS_IO_READ  16h    ; BX=fd, DI=buffer, CX=count; returns AX=bytes read, CF on error
        %assign SYS_IO_WRITE 17h    ; BX=fd, SI=buffer, CX=count; returns AX=bytes written, CF on error

        %assign SYS_NET_ARP 20h
        %assign SYS_NET_INIT 21h
        %assign SYS_NET_PING 22h
        %assign SYS_NET_RECV 23h
        %assign SYS_NET_SEND 24h
        %assign SYS_NET_UDP_RECV 25h
        %assign SYS_NET_UDP_SEND 26h

        %assign SYS_RTC_DATETIME 30h
        %assign SYS_RTC_UPTIME 31h

        %assign SYS_SCR_CLEAR 40h

        %assign SYS_EXEC 0F0h
        %assign SYS_EXIT 0F1h
        %assign SYS_REBOOT 0F2h
        %assign SYS_SHUTDOWN 0F3h
