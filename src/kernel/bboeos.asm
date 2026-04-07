        org 7C00h               ; offset where bios loads our first stage
        %include "constants.asm"
        %assign STAGE2_SECTORS (DIR_SECTOR - 2)

start:
        ;; Set initial state
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov [boot_disk], dl

        mov ax, 50h             ; Linear 0x500 is start of free space
        cli                     ; Disable interrupts while adjusting stack
        mov ss, ax
        mov sp, 7700h           ; 0050h:7700h is equivalent to 0x7c00
        sti                     ; Enable interrupts

        call clear_screen
        mov si, WELCOME
        call put_string

        xor ax, ax
        int 13h                 ; reset disk
        jc .error

        ;; Query drive geometry for LBA-to-CHS conversion
        push es                 ; INT 13h AH=08h clobbers ES:DI
        mov ah, 08h
        mov dl, [boot_disk]
        int 13h                 ; CH=max cyl low, CL=max sector|cyl high, DH=max head
        pop es
        jc .default_geo         ; fallback if query fails
        mov al, cl
        and al, 3Fh             ; sectors per track (bits 0-5 of CL)
        mov [sectors_per_track], al
        inc dh                  ; max head is 0-based, convert to count
        mov [heads_per_cyl], dh
        jmp .geo_done
        .default_geo:
        mov byte [sectors_per_track], 63
        mov byte [heads_per_cyl], 16
        .geo_done:

        mov ax, 0200h | STAGE2_SECTORS
        mov bx, 7E00h           ; 0x7C00 + 512
        mov cx, 2               ; start at cylinder 0 sector 2
        mov dh, 0               ; start at head 0
        mov dl, [boot_disk]
        int 13h                 ; read

        jc .error
        cmp al, STAGE2_SECTORS
        jne .error

        xor ah, ah
        int 1Ah                 ; CX:DX = ticks since midnight
        mov [boot_ticks_low], dx
        mov [boot_ticks_high], cx
        call install_syscalls
        jmp boot_shell

        .error:
        mov si, DISK_FAILURE
        call put_string

        .halt:
        hlt
        jmp .halt

clear_screen:
        push ax
        mov ax, 03h
        int 10h                 ; Set 80x25 color text mode
        pop ax
        ret

%include "ansi.asm"

        ;; Variables
        boot_disk db 0
        heads_per_cyl db 16
        sectors_per_track db 63

        ;; Strings
        DISK_FAILURE db `Disk failure\n\0`
        WELCOME db `Welcome to BBoeOS!\nVersion 0.4.0 (2026/03/28)\n\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature


boot_shell:
        ;; Load shell program from filesystem
        mov si, SHELL_NAME
        call find_file
        jc .no_shell

        mov di, PROGRAM_BASE
        call load_file
        jc .no_shell

        mov [shell_sp], sp
        jmp PROGRAM_BASE

        .no_shell:
        mov si, SHELL_ERROR
        call put_string
        .shell_halt:
        hlt
        jmp .shell_halt

%include "io.asm"
%include "net.asm"
%include "syscall.asm"
%include "system.asm"

        ;; Values
        boot_ticks_high  dw 0
        boot_ticks_low   dw 0
        serial_pb_buf    db 0, 0 ; serial pushback buffer (up to 2 bytes)
        serial_pb_count  db 0    ; number of bytes in pushback buffer
        shell_sp dw 0

        ;; Strings
        SHELL_ERROR db `Shell not found\n\0`
        SHELL_NAME db `bin/shell\0`
