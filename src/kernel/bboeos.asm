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

        ;; Strings
        DISK_FAILURE db `Disk failure\r\n\0`
        WELCOME db `Welcome to BBoeOS!\r\nVersion 0.3.0 (2026/03/27)\r\n\0`

        ;; End of MBR
        times 510-($-$$) db 0   ; Pad remainder of boot sector with 0s
        dw 0AA55h               ; The standard PC boot signature


boot_shell:
        ;; Load shell program from filesystem
        mov si, SHELL_NAME
        call find_file
        jc .no_shell

        mov cx, [bx+14]        ; File size in bytes
        mov bl, [bx+12]        ; Start sector
        mov di, PROGRAM_BASE    ; Destination

.load_sector:
        mov al, bl
        call read_sector
        jc .no_shell

        ;; Copy sector from DISK_BUFFER to destination
        push cx
        cmp cx, 512
        jle .partial
        mov cx, 256             ; Full sector = 256 words
        jmp .copy
.partial:
        inc cx                  ; Round up to whole words
        shr cx, 1
.copy:
        cld
        mov si, DISK_BUFFER
        rep movsw
        pop cx

        sub cx, 512
        jle .loaded
        inc bl                  ; Next sector
        jmp .load_sector

.loaded:
        mov [shell_sp], sp
        jmp PROGRAM_BASE

        .no_shell:
        mov si, SHELL_ERROR
        call put_string
        .shell_halt:
        hlt
        jmp .shell_halt

%include "io.asm"
%include "syscall.asm"
%include "system.asm"

        ;; Values
        boot_ticks_high dw 0
        boot_ticks_low  dw 0
        shell_sp dw 0

        ;; Strings
        SHELL_ERROR db `Shell not found\r\n\0`
        SHELL_NAME db `shell\0`
