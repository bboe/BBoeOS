        %assign BUFFER 500h
        %assign DIR_ENTRY_SIZE 16
        %assign DIR_MAX_ENTRIES 32
        %assign DIR_SECTOR 10
        %assign DISK_BUFFER 9000h     ; 512 bytes (one sector)
        %assign ERR_DIR_FULL  01h     ; Copy error: no free directory entries
        %assign ERR_EXISTS    02h     ; Rename/copy error: destination name already exists
        %assign ERR_NOT_EXEC  03h     ; Exec error: file exists but is not executable
        %assign ERR_NOT_FOUND 04h     ; File not found
        %assign ERR_PROTECTED 05h     ; Rename/chmod error: file is protected
        %assign EXEC_ARG 4FEh
        %assign FLAG_EXEC 01h         ; Directory entry flags: bit 0 = executable
        %assign MAX_INPUT 256
        %assign NE2K_BASE 300h
        %assign NET_TX_BUF 9200h     ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign NET_RX_BUF 9800h     ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign PROGRAM_BASE 6000h

        ;; Syscall numbers (INT 30h, passed in AH)
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
