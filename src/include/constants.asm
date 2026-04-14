        %assign BUFFER 500h
        %assign DIRECTORY_ENTRY_SIZE 32
        %assign DIRECTORY_MAX_ENTRIES 48
        %assign DIRECTORY_NAME_LENGTH 25         ; 24 chars + null
        %assign DIRECTORY_OFFSET_FLAGS (DIRECTORY_NAME_LENGTH)
        %assign DIRECTORY_OFFSET_SECTOR (DIRECTORY_NAME_LENGTH + 1)
        %assign DIRECTORY_OFFSET_SIZE (DIRECTORY_NAME_LENGTH + 3)   ; 32-bit (4 bytes)
        %assign DIRECTORY_SECTOR 13
        %assign DIRECTORY_SECTORS 3
        %assign SECTOR_BUFFER 0E000h    ; 512 bytes (one sector)
        %assign ERROR_DIRECTORY_FULL  01h     ; Copy error: no free directory entries
        %assign ERROR_EXISTS    02h     ; Rename/copy error: destination name already exists
        %assign ERROR_NOT_EXECUTE  03h     ; Exec error: file exists but is not executable
        %assign ERROR_NOT_FOUND 04h     ; File not found
        %assign ERROR_PROTECTED 05h     ; Rename/chmod error: file is protected
        %assign EXEC_ARG 4FEh
        %assign FD_ENTRY_SIZE 32
        %assign FD_MAX 8
        %assign FD_OFFSET_DIRECTORY_OFFSET 14    ; offset of dir_off field within FD entry
        %assign FD_OFFSET_DIRECTORY_SECTOR 12    ; offset of dir_sec field within FD entry
        %assign FD_OFFSET_FLAGS 1       ; offset of flags field within FD entry
        %assign FD_OFFSET_MODE 16       ; offset of mode field (file permission flags)
        %assign FD_OFFSET_POSITION 8         ; offset of pos field within FD entry (32-bit)
        %assign FD_OFFSET_SIZE 4        ; offset of size field within FD entry (32-bit)
        %assign FD_OFFSET_START 2       ; offset of start_sec field within FD entry
        %assign FD_OFFSET_TYPE 0        ; offset of type field within FD entry
        %assign FD_TYPE_CONSOLE 2
        %assign FD_TYPE_DIRECTORY 3
        %assign FD_TYPE_FILE 1
        %assign FD_TYPE_FREE 0
        %assign FLAG_DIRECTORY  02h         ; Directory entry flags: bit 1 = subdirectory
        %assign FLAG_EXECUTE 01h         ; Directory entry flags: bit 0 = executable
        %assign FUNCTION_TABLE 7E00h    ; Start of kernel jump table (3 bytes per entry)
        %assign FUNCTION_DIE            FUNCTION_TABLE      ; SI=msg, CX=len: write to stdout then exit
        %assign FUNCTION_EXIT           FUNCTION_DIE + 3    ; Exit program (reload shell)
        %assign FUNCTION_GET_CHARACTER  FUNCTION_EXIT + 3   ; Read one byte from stdin; returns AL
        %assign FUNCTION_PARSE_ARGV   FUNCTION_GET_CHARACTER + 3 ; DI=argv buf: split EXEC_ARG, CX=argc
        %assign FUNCTION_PRINT_BCD     FUNCTION_PARSE_ARGV + 3 ; AL=BCD byte: print two BCD digits
        %assign FUNCTION_PRINT_BYTE_DECIMAL FUNCTION_PRINT_BCD + 3 ; AL=byte: print 1-3 decimal digits
        %assign FUNCTION_PRINT_CHARACTER FUNCTION_PRINT_BYTE_DECIMAL + 3 ; AL=char: print to stdout
        %assign FUNCTION_PRINT_DECIMAL FUNCTION_PRINT_CHARACTER + 3 ; AL=byte: print 2 zero-padded decimal digits
        %assign FUNCTION_PRINT_HEX    FUNCTION_PRINT_DECIMAL + 3 ; AL=byte: print 2 hex digits
        %assign FUNCTION_PRINT_IP      FUNCTION_PRINT_HEX + 3 ; SI=4-byte IP: print dotted decimal
        %assign FUNCTION_PRINT_MAC     FUNCTION_PRINT_IP + 3 ; SI=6-byte MAC: print XX:XX:XX:XX:XX:XX
        %assign FUNCTION_PRINT_STRING  FUNCTION_PRINT_MAC + 3 ; DI=null-terminated string: write to stdout
        %assign FUNCTION_WRITE_STDOUT  FUNCTION_PRINT_STRING + 3 ; SI=buf, CX=len: write to stdout
        %assign MAX_INPUT 256
        %assign NE2K_BASE 300h
        %assign NET_RECEIVE_BUFFER 0E800h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign NET_TRANSMIT_BUFFER 0E200h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
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
        %assign SYS_IO_OPEN  12h    ; SI=filename, AL=flags, DL=mode; returns AX=fd, CF on error
        %assign SYS_IO_READ  13h    ; BX=fd, DI=buffer, CX=count; returns AX=bytes read, CF on error
        %assign SYS_IO_WRITE 14h    ; BX=fd, SI=buffer, CX=count; returns AX=bytes written, CF on error

        %assign SYS_NET_ARP 20h
        %assign SYS_NET_INIT 21h
        %assign SYS_NET_PING 22h
        %assign SYS_NET_RECEIVE 23h
        %assign SYS_NET_SEND 24h
        %assign SYS_NET_UDP_RECEIVE 25h
        %assign SYS_NET_UDP_SEND 26h

        %assign SYS_RTC_DATETIME 30h
        %assign SYS_RTC_UPTIME 31h

        %assign SYS_SCREEN_CLEAR 40h

        %assign SYS_EXEC 0F0h
        %assign SYS_EXIT 0F1h
        %assign SYS_REBOOT 0F2h
        %assign SYS_SHUTDOWN 0F3h
