        %assign ARGV 4DEh               ; 32 bytes (16 word-sized pointers)
        %assign BSS_MAGIC 0B055h
        %assign BUFFER 500h
        %assign DIRECTORY_ENTRY_SIZE 32
        %assign DIRECTORY_MAX_ENTRIES 48
        %assign DIRECTORY_NAME_LENGTH 25         ; 24 chars + null
        %assign DIRECTORY_OFFSET_FLAGS (DIRECTORY_NAME_LENGTH)
        %assign DIRECTORY_OFFSET_SECTOR (DIRECTORY_NAME_LENGTH + 1)
        %assign DIRECTORY_OFFSET_SIZE (DIRECTORY_NAME_LENGTH + 3)   ; 32-bit (4 bytes)
        %assign DIRECTORY_SECTORS 3
        %assign EDIT_BUFFER_BASE 2000h       ; Edit gap-buffer start (6.5 KB after PROGRAM_BASE)
        %assign EDIT_BUFFER_SIZE 5200h       ; Gap buffer size (EDIT_KILL_BUFFER - EDIT_BUFFER_BASE)
        %assign EDIT_KILL_BUFFER 7200h       ; Kill buffer start (7C00h - EDIT_KILL_BUFFER_SIZE)
        %assign EDIT_KILL_BUFFER_SIZE 0A00h     ; Kill buffer size (2560 bytes)
        %assign ERROR_DIRECTORY_FULL  01h     ; Copy error: no free directory entries
        %assign ERROR_EXISTS    02h     ; Rename/copy error: destination name already exists
        %assign ERROR_NOT_EMPTY 06h     ; Rmdir error: directory is not empty
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
        %assign FD_TYPE_FREE 0      ; Must stay 0: fd_init zeroes the table; fd_alloc treats 0 as free
        %assign FD_TYPE_CONSOLE 1
        %assign FD_TYPE_DIRECTORY 2
        %assign FD_TYPE_FILE 3
        %assign FD_TYPE_ICMP 4
        %assign FD_TYPE_NET 5
        %assign FD_TYPE_UDP 6
        %assign FD_TYPE_VGA 7
        %assign FLAG_DIRECTORY  02h         ; Directory entry flags: bit 1 = subdirectory
        %assign FLAG_EXECUTE 01h         ; Directory entry flags: bit 0 = executable
        %assign FUNCTION_TABLE 7E00h    ; Start of kernel jump table (3 bytes per entry)
        %assign FUNCTION_DIE            FUNCTION_TABLE      ; SI=msg, CX=len: write to stdout then exit
        %assign FUNCTION_EXIT           FUNCTION_DIE + 3    ; Exit program (reload shell)
        %assign FUNCTION_GET_CHARACTER  FUNCTION_EXIT + 3   ; Read one byte from stdin; returns AL
        %assign FUNCTION_PARSE_ARGV   FUNCTION_GET_CHARACTER + 3 ; DI=argv buf: split EXEC_ARG, CX=argc
        %assign FUNCTION_PRINT_BYTE_DECIMAL FUNCTION_PARSE_ARGV + 3 ; AL=byte: print 1-3 decimal digits
        %assign FUNCTION_PRINT_CHARACTER FUNCTION_PRINT_BYTE_DECIMAL + 3 ; AL=char: print to stdout
        %assign FUNCTION_PRINT_DATETIME FUNCTION_PRINT_CHARACTER + 3 ; DX:AX=epoch seconds: print YYYY-MM-DD HH:MM:SS
        %assign FUNCTION_PRINT_DECIMAL FUNCTION_PRINT_DATETIME + 3 ; AL=byte: print 2 zero-padded decimal digits
        %assign FUNCTION_PRINT_HEX    FUNCTION_PRINT_DECIMAL + 3 ; AL=byte: print 2 hex digits
        %assign FUNCTION_PRINT_IP      FUNCTION_PRINT_HEX + 3 ; SI=4-byte IP: print dotted decimal
        %assign FUNCTION_PRINT_MAC     FUNCTION_PRINT_IP + 3 ; SI=6-byte MAC: print XX:XX:XX:XX:XX:XX
        %assign FUNCTION_PRINT_STRING  FUNCTION_PRINT_MAC + 3 ; DI=null-terminated string: write to stdout
        %assign FUNCTION_PRINTF       FUNCTION_PRINT_STRING + 3 ; cdecl: push args R-to-L, push fmt, call
        %assign FUNCTION_WRITE_STDOUT  FUNCTION_PRINTF + 3 ; SI=buf, CX=len: write to stdout
        %assign IPPROTO_ICMP 1          ; Protocol argument to net_open for SOCK_DGRAM ICMP sockets
        %assign IPPROTO_UDP 17          ; Protocol argument to net_open for SOCK_DGRAM UDP sockets
        %assign MAX_INPUT 256
        %assign NE2K_BASE 300h
        %assign NET_RECEIVE_BUFFER 0E800h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign NET_TRANSMIT_BUFFER 0E200h    ; 1536 bytes (max Ethernet frame: 1500 MTU + 14 header + padding)
        %assign NULL 0
        %assign O_CREAT  10h
        %assign O_RDONLY 00h
        %assign O_TRUNC  20h
        %assign O_WRONLY 01h
        %assign PROGRAM_BASE 0600h
        %assign SECTOR_BUFFER 0E000h    ; 512 bytes (one sector)
        %assign SOCK_DGRAM 1
        %assign SOCK_RAW 0
        %assign STDERR 2
        %assign STDIN 0
        %assign STDOUT 1

        ;; Syscall numbers (INT 30h, passed in AH)
        %assign SYS_FS_CHMOD  00h
        %assign SYS_FS_MKDIR  01h
        %assign SYS_FS_RENAME 02h
        %assign SYS_FS_RMDIR  03h
        %assign SYS_FS_UNLINK 04h

        %assign SYS_IO_CLOSE 10h    ; BX=fd; CF on error
        %assign SYS_IO_FSTAT 11h    ; BX=fd; returns AL=mode, CX:DX=size (32-bit), CF on error
        %assign SYS_IO_IOCTL 12h    ; BX=fd, AL=cmd, other regs per (fd_type,cmd); CF on error
        %assign SYS_IO_OPEN  13h    ; SI=filename, AL=flags, DL=mode; returns AX=fd, CF on error
        %assign SYS_IO_READ  14h    ; BX=fd, DI=buffer, CX=count; returns AX=bytes read, CF on error
        %assign SYS_IO_WRITE 15h    ; BX=fd, SI=buffer, CX=count; returns AX=bytes written, CF on error

        %assign SYS_NET_MAC 20h
        %assign SYS_NET_OPEN 21h
        %assign SYS_NET_RECVFROM 22h
        %assign SYS_NET_SENDTO 23h
        %assign SYS_RTC_DATETIME 30h    ; returns DX:AX = unsigned epoch seconds (1970-01-01 UTC)
        %assign SYS_RTC_MILLIS 31h      ; returns DX:AX = milliseconds since boot
        %assign SYS_RTC_SLEEP 32h       ; CX=milliseconds: busy-wait via the PIT tick counter
        %assign SYS_RTC_UPTIME 33h      ; returns AX = seconds since boot

        %assign SYS_EXEC 0F0h
        %assign SYS_EXIT 0F1h
        %assign SYS_REBOOT 0F2h
        %assign SYS_SHUTDOWN 0F3h

        ;; VGA ioctl commands (SYS_IO_IOCTL AL on fd of type FD_TYPE_VGA)
        %assign VGA_IOCTL_FILL_BLOCK    00h  ; CL=col, CH=row, DL=color (mode 13h 8x8 tile)
        %assign VGA_IOCTL_MODE          01h  ; DL=mode; also clears screen and serial
        %assign VGA_IOCTL_SET_PALETTE   02h  ; CL=index, CH=r, DL=g, DH=b (6-bit DAC)

        ;; Video modes (DL argument to VGA_IOCTL_MODE; INT 10h AH=00h AL)
        %assign VIDEO_MODE_TEXT_40x25      01h  ; 40x25 color text
        %assign VIDEO_MODE_TEXT_80x25      03h  ; 80x25 color text (default)
        %assign VIDEO_MODE_CGA_320x200     04h  ; CGA 4-color 320x200
        %assign VIDEO_MODE_CGA_640x200     06h  ; CGA 2-color 640x200
        %assign VIDEO_MODE_EGA_320x200_16  0Dh  ; EGA 16-color 320x200
        %assign VIDEO_MODE_EGA_640x200_16  0Eh  ; EGA 16-color 640x200
        %assign VIDEO_MODE_EGA_640x350_16  10h  ; EGA 16-color 640x350
        %assign VIDEO_MODE_VGA_640x480_16  12h  ; VGA 16-color 640x480
        %assign VIDEO_MODE_VGA_320x200_256 13h  ; VGA 256-color 320x200
