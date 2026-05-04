        %assign ARGV 14DEh              ; 32 bytes (16 word-sized pointers); = USER_DATA_BASE + 0x4DE
        %assign BOOT_STASH_OFFSET 2     ; offset within kernel.bin of boot_disk (db) followed by directory_sector (dw); written by boot.asm post-load and read by the kernel through the direct map.  Layout contract: kernel.asm's first instruction is `jmp short high_entry` which skips past these bytes.
        %assign BSS_MAGIC 0B055h        ; Legacy 4-byte trailer (dw bss_size; dw 0xB055)
        %assign BSS_MAGIC32 0B032h      ; New 6-byte trailer (dd bss_size; dw 0xB032), 4 GB max
        %assign BUFFER 1500h            ; 256 bytes; = USER_DATA_BASE + 0x500
        %assign DIRECTORY_ENTRY_SIZE 32
        %assign DIRECTORY_MAX_ENTRIES 48
        %assign DIRECTORY_NAME_LENGTH 25         ; 24 chars + null
        %assign DIRECTORY_OFFSET_FLAGS (DIRECTORY_NAME_LENGTH)
        %assign DIRECTORY_OFFSET_SECTOR (DIRECTORY_NAME_LENGTH + 1)
        %assign DIRECTORY_OFFSET_SIZE (DIRECTORY_NAME_LENGTH + 3)   ; 32-bit (4 bytes)
        %assign DIRECTORY_SECTORS 3
        %assign ERROR_DIRECTORY_FULL  01h     ; Copy error: no free directory entries
        %assign ERROR_EXISTS    02h     ; Rename/copy error: destination name already exists
        %assign ERROR_FAULT     07h     ; Bad user pointer: out of user range, wraps, or filename has no NUL within MAX_PATH
        %assign ERROR_NOT_EMPTY 06h     ; Rmdir error: directory is not empty
        %assign ERROR_NOT_EXECUTE  03h     ; Exec error: file exists but is not executable
        %assign ERROR_NOT_FOUND 04h     ; File not found
        %assign ERROR_PROTECTED 05h     ; Rename/chmod error: file is protected
        %assign EXEC_ARG 14FCh          ; 4 bytes (dword pointer under --bits 32); before BUFFER; = USER_DATA_BASE + 0x4FC
        %assign FD_ENTRY_SIZE 64
        ;; Per-fd PS/2 event ring (FD_TYPE_CONSOLE only).  Events are
        ;; (pressed << 16) | bbkey, 32-bit slots.  Linux's evdev pattern:
        ;; each readable console fd gets its own queue, populated by
        ;; the PS/2 IRQ broadcaster (drivers/ps2.c) and drained by
        ;; CONSOLE_IOCTL_TRY_GET_EVENT (fs/fd/console.c).  The queue
        ;; lives inline in the fd entry so it dies with fd_close /
        ;; fd_init — no global state to drain across program boundaries.
        ;; Length must be a power of 2 for the head/tail mask.
        %assign FD_EVENT_QUEUE_LEN 8
        %assign FD_MAX 8
        %assign FD_OFFSET_DIRECTORY_OFFSET 14    ; offset of dir_off field within FD entry
        %assign FD_OFFSET_DIRECTORY_SECTOR 12    ; offset of dir_sec field within FD entry
        %assign FD_OFFSET_EVENT_BUF 20  ; FD_EVENT_QUEUE_LEN * 4 bytes; 4-aligned for dword loads
        %assign FD_OFFSET_EVENT_HEAD 17 ; ring read cursor (uint8); == TAIL means empty
        %assign FD_OFFSET_EVENT_TAIL 18 ; ring write cursor (uint8); (TAIL+1)&mask == HEAD means full
        %assign FD_OFFSET_FLAGS 1       ; offset of flags field within FD entry
        %assign FD_OFFSET_MODE 16       ; offset of mode field (file permission flags)
        %assign FD_OFFSET_POSITION 8         ; offset of pos field within FD entry (32-bit)
        %assign FD_OFFSET_SIZE 4        ; offset of size field within FD entry (32-bit)
        %assign FD_OFFSET_START 2       ; offset of start_sec field within FD entry
        %assign FD_OFFSET_TYPE 0        ; offset of type field within FD entry
        %assign FD_TYPE_AUDIO 1     ; SB16 PCM stream (/dev/audio); see drivers/sb16.c
        %assign FD_TYPE_CONSOLE 2
        %assign FD_TYPE_DIRECTORY 3
        %assign FD_TYPE_FILE 4
        %assign FD_TYPE_FREE 0      ; Must stay 0: fd_init zeroes the table; fd_alloc treats 0 as free
        %assign FD_TYPE_ICMP 5
        %assign FD_TYPE_NET 6
        %assign FD_TYPE_UDP 7
        %assign FD_TYPE_VGA 8
        %assign FLAG_DIRECTORY  02h         ; Directory entry flags: bit 1 = subdirectory
        %assign FLAG_EXECUTE 01h         ; Directory entry flags: bit 0 = executable
        ;; vDSO FUNCTION_TABLE base + 5-byte slots.  Slot offsets must
        ;; match the function_table jmp order in src/vdso/vdso.asm.
        ;; FUNCTION_TABLE comes first as the base anchor; the rest are
        ;; sorted alphabetically with explicit slot offsets so adding /
        ;; reordering an entry only touches its own line.
        %assign FUNCTION_TABLE 00010000h ; vDSO code page; kernel copies vdso.bin here at boot
        %assign FUNCTION_DIE                FUNCTION_TABLE +  0 ; SI=msg, CX=len: write to stdout then exit
        %assign FUNCTION_EXIT               FUNCTION_TABLE +  5 ; Exit program (reload shell)
        %assign FUNCTION_GET_CHARACTER      FUNCTION_TABLE + 10 ; Read one byte from stdin; returns AL
        %assign FUNCTION_PARSE_ARGV         FUNCTION_TABLE + 15 ; DI=argv buf: split EXEC_ARG, CX=argc
        %assign FUNCTION_PRINT_BYTE_DECIMAL FUNCTION_TABLE + 20 ; AL=byte: print 1-3 decimal digits
        %assign FUNCTION_PRINT_CHARACTER    FUNCTION_TABLE + 25 ; AL=char: print to stdout
        %assign FUNCTION_PRINT_DATETIME     FUNCTION_TABLE + 30 ; DX:AX=epoch seconds: print YYYY-MM-DD HH:MM:SS
        %assign FUNCTION_PRINT_DECIMAL      FUNCTION_TABLE + 35 ; AL=byte: print 2 zero-padded decimal digits
        %assign FUNCTION_PRINT_HEX          FUNCTION_TABLE + 40 ; AL=byte: print 2 hex digits
        %assign FUNCTION_PRINT_IP           FUNCTION_TABLE + 45 ; SI=4-byte IP: print dotted decimal
        %assign FUNCTION_PRINT_MAC           FUNCTION_TABLE + 50 ; SI=6-byte MAC: print XX:XX:XX:XX:XX:XX
        %assign FUNCTION_PRINT_STRING       FUNCTION_TABLE + 55 ; DI=null-terminated string: write to stdout
        %assign FUNCTION_PRINTF             FUNCTION_TABLE + 60 ; cdecl: push args R-to-L, push fmt, call
        %assign FUNCTION_WRITE_STDOUT       FUNCTION_TABLE + 65 ; SI=buf, CX=len: write to stdout
        %assign IPPROTO_ICMP 1          ; Protocol argument to net_open for SOCK_DGRAM ICMP sockets
        %assign IPPROTO_UDP 17          ; Protocol argument to net_open for SOCK_DGRAM UDP sockets
        %assign KERNEL_VIRT_BASE 0FF800000h     ; Lowest kernel-virt address.  User pointers + lengths must stay strictly below this; idt.asm's user-fault triage and access_ok both gate on it.  Equals USER_STACK_TOP and DIRECT_MAP_BASE — all three move in lockstep.
        %assign MAX_INPUT 256
        %assign MAX_PATH 64             ; Hard cap on user-supplied filename byte count (incl. NUL); enough for "<24-char dir>/<24-char file>" plus headroom
        %assign NE2K_BASE 300h
        %assign NULL 0
        %assign O_CREAT  10h
        %assign O_RDONLY 00h
        %assign O_TRUNC  20h
        %assign O_WRONLY 01h
        ;; 8259A PIC ports + EOI byte.  Used by the boot path's pic_remap
        ;; sequence and by the kernel-side IRQ handlers / drivers.
        %assign PIC1_CMD_PORT   0x20
        %assign PIC1_DATA_PORT  0x21
        %assign PIC2_CMD_PORT   0xA0
        %assign PIC2_DATA_PORT  0xA1
        %assign PIC_EOI         0x20
        %assign PROGRAM_BASE 08048000h          ; user-virt program load address (Linux ELF convention)
        ;; Sound Blaster 16 (ISA) at QEMU's `-device sb16` defaults — base 0x220.
        ;; C drivers/sb16.c uses bare integers for the offset registers (matches
        ;; the rtc.c / ne2k.c convention — cc.py emits #define as %define which
        ;; would clash with these %assigns).  Reference table:
        ;;   SB16_BASE              = 0x220
        ;;   DSP_RESET (W)          = +0x06   write 1, wait, write 0
        ;;   DSP_DATA (R)           = +0x0A   read DSP responses
        ;;   DSP_WRITE (W)          = +0x0C   command + data byte writes
        ;;   DSP_WRITE_STATUS (R)   = +0x0C   bit 7 high = DSP write buffer full
        ;;   DSP_READ_STATUS (R)    = +0x0E   bit 7 high = DSP_DATA has data;
        ;;                                    ALSO acks 8-bit IRQ on read
        ;;   MIXER_INDEX (W)        = +0x04
        ;;   MIXER_DATA (R/W)       = +0x05
        %assign SB16_BASE             0x220
        %assign SB16_DSP_READ_STATUS  0x22E   ; referenced from asm IRQ 5 handler
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
        %assign SYS_IO_SEEK  15h    ; BX=fd, ECX=offset, AL=whence (0/1/2); returns EAX=new position, CF on error
        %assign SYS_IO_WRITE 16h    ; BX=fd, SI=buffer, CX=count; returns AX=bytes written, CF on error

        ;; SEEK_* whence values — passed in AL of SYS_IO_SEEK.  Match POSIX so
        ;; libc lseek can pass the user value through unchanged.
        %assign SEEK_SET 0
        %assign SEEK_CUR 1
        %assign SEEK_END 2

        %assign SYS_NET_MAC 20h
        %assign SYS_NET_OPEN 21h
        %assign SYS_NET_RECVFROM 22h
        %assign SYS_NET_SENDTO 23h
        %assign SYS_RTC_DATETIME 30h    ; returns DX:AX = unsigned epoch seconds (1970-01-01 UTC)
        %assign SYS_RTC_MILLIS 31h      ; returns DX:AX = milliseconds since boot
        %assign SYS_RTC_SLEEP 32h       ; CX=milliseconds: busy-wait via the PIT tick counter
        %assign SYS_RTC_UPTIME 33h      ; returns AX = seconds since boot

        %assign SYS_VIDEO_MAP    40h    ; (none); returns EAX = user-virt of mode-13h FB, CF on OOM

        %assign SYS_SYS_BREAK 0F0h        ; EBX = new break (0 = query); returns EAX = resulting break, CF=0
        %assign SYS_SYS_EXEC 0F1h
        %assign SYS_SYS_EXIT 0F2h
        %assign SYS_SYS_REBOOT 0F3h
        %assign SYS_SYS_SHUTDOWN 0F4h

        %assign TSS_SELECTOR 28h        ; GDT[5]: 32-bit available TSS, DPL=0
        %assign USER_CODE_SELECTOR 1Bh  ; GDT[3] | RPL=3: ring-3 code segment (flat 4 GB)
        %assign USER_DATA_BASE 1000h    ; user-virt of the shell↔program handoff frame (ARGV / EXEC_ARG / BUFFER); PTE[0] (virt 0..0xFFF) stays unmapped so NULL deref faults
        %assign USER_DATA_SELECTOR 23h  ; GDT[4] | RPL=3: ring-3 data segment (flat 4 GB)
        %assign USER_STACK_TOP 0FF800000h       ; Ring-3 stack top (one past last user-virt page); 64 KB stack at 0xFF7F0000-0xFF800000, 64 KB guard at 0xFF7E0000-0xFF7F0000.  Top sits exactly at the user/kernel boundary so ESP=USER_STACK_TOP can push 4 B into [0xFF7FFFFC, 0xFF800000) without crossing into the kernel half.

        ;; PIT constants used by entry.asm's IRQ 0 hookup and rtc.c's
        ;; PIT-driven sleep / tick counter.  PIC_EOI lives above with
        ;; the rest of the 8259A constants.
        %assign PIT_CHANNEL0       0x40
        %assign PIT_COMMAND        0x43
        %assign PIT_DIVISOR        11932         ; 1193182 / 11932 ≈ 99.998 Hz
        %assign PIT_MODE2_LOHI_CH0 00110100b     ; ch0, lo/hi access, mode 2, binary
        %assign MS_PER_TICK        10
        %assign TICKS_PER_SECOND   100

        ;; VGA hardware register ports (used by both the real-mode boot
        ;; path's vga_font_load and the post-flip vga driver).
        %assign VGA_GC_DATA     03CFh
        %assign VGA_GC_INDEX    03CEh
        %assign VGA_SEQ_DATA    03C5h
        %assign VGA_SEQ_INDEX   03C4h

        ;; Console ioctl commands (SYS_IO_IOCTL AL on fd of type FD_TYPE_CONSOLE).
        ;; Both are non-blocking peeks into the keyboard input streams.
        %assign CONSOLE_IOCTL_TRY_GETC      00h  ; AX = ASCII byte (0 if empty)
        %assign CONSOLE_IOCTL_TRY_GET_EVENT 01h  ; EAX = (pressed<<16)|bbkey (0 if empty)

        ;; VGA ioctl commands (SYS_IO_IOCTL AL on fd of type FD_TYPE_VGA)
        %assign VGA_IOCTL_FILL_BLOCK    00h  ; CL=col, CH=row, DL=color (mode 13h 8x8 tile)
        %assign VGA_IOCTL_MODE          01h  ; DL=mode; also clears screen and serial
        %assign VGA_IOCTL_SET_PALETTE   02h  ; CL=index, CH=r, DL=g, DH=b (6-bit DAC)

        ;; Audio ioctl commands (SYS_IO_IOCTL AL on fd of type FD_TYPE_AUDIO).
        %assign AUDIO_IOCTL_QUERY       00h  ; AX = 1 if SB16 present, 0 otherwise

        ;; Video modes (DL argument to VGA_IOCTL_MODE; INT 10h AH=00h AL).
        ;; Only the two modes that programs actually switch between are
        ;; defined here; the BIOS supports more (CGA, EGA, VGA 16-color
        ;; etc.) and `vga_set_mode` will pass through any AL value, but
        ;; nothing in the tree currently asks for them.
        %assign VIDEO_MODE_TEXT_80x25      03h  ; 80x25 color text (default)
        %assign VIDEO_MODE_VGA_320x200_256 13h  ; VGA 256-color 320x200

        ;; Mode-13h framebuffer placement.  SYS_VIDEO_MAP exposes the
        ;; physical aperture at MODE13H_PHYS into the calling program's
        ;; PD at MODE13H_USER_VIRT, RW + U/S=1.  The framebuffer is
        ;; 320*200 = 64000 bytes (8-bit indexed colour) — fits in 16
        ;; pages; the trailing ~1.5 KB past the FB end is harmless VGA
        ;; aperture RAM.
        %assign MODE13H_BYTES     320 * 200   ; 64000 bytes (16 pages worth)
        %assign MODE13H_PHYS      0A0000h     ; physical address of the mode-13h framebuffer
        %assign MODE13H_USER_VIRT 0B8000000h  ; user-virt slot where SYS_VIDEO_MAP exposes the framebuffer
