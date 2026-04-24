;;; -----------------------------------------------------------------------
;;; Kernel jump table: stable entry points for shared utility functions.
;;; Programs call these at fixed addresses (e.g., call FUNCTION_WRITE_STDOUT).
;;; Each entry is a 3-byte jmp near to the actual implementation.
;;; Table must stay sorted alphabetically to match constants.asm.
;;; -----------------------------------------------------------------------
        jmp near shared_die
        jmp near shared_exit
        jmp near shared_get_character
        jmp near shared_parse_argv
        jmp near shared_print_byte_decimal
        jmp near shared_print_character
        jmp near shared_print_datetime
        jmp near shared_print_decimal
        jmp near shared_print_hex
        jmp near shared_print_ip
        jmp near shared_print_mac
        jmp near shared_print_string
        jmp near shared_printf
        jmp near shared_write_stdout

boot_shell:
        call kernel_init        ; PIC remap, PIT + IRQ 0, INT 30h gate, NIC probe

        mov si, WELCOME
        call put_string

        call vga_font_load      ; load ROM 8x16 font into plane 2 offset 0x4000 before any mode 13h switch corrupts plane 2
        call ps2_init           ; mask BIOS IRQ 1 before anyone reads keys
        cmp byte [boot_disk], 80h
        jae .post_fdc
        call fdc_init           ; reset, motor, recalibrate drive A:
        .post_fdc:
        call vfs_init
        call idt_install        ; 32-bit IDT: exceptions + INT 30h gate
        jmp enter_protected_mode ; never returns; resumes at protected_mode_entry

shell_reload:
        ;; Re-entry point for SYS_EXIT: reload the shell binary without
        ;; repeating one-time boot work (kernel_init, WELCOME, driver inits).
        call fd_init
        ;; Load shell program from filesystem
        mov si, SHELL_NAME
        call vfs_find           ; populates vfs_found_*
        jc .no_shell
        mov di, PROGRAM_BASE
        call vfs_load           ; DI=dest → CF
        jc .no_shell
        call bss_setup

        mov [shell_sp], sp
        jmp PROGRAM_BASE

        .no_shell:
        mov si, SHELL_ERROR
        call put_string
        .shell_halt:
        hlt
        jmp .shell_halt

bss_setup:
        ;; Zero the BSS region of the freshly-loaded program.
        ;; Reads binary size from vfs_found_size, checks for the 4-byte
        ;; trailer (dw bss_size; dw BSS_MAGIC) at the end, then zeroes
        ;; bss_size bytes starting immediately after the binary.
        push ax
        push cx
        push di
        mov di, PROGRAM_BASE
        add di, [vfs_found_size]        ; DI = PROGRAM_BASE + binary_size
        cmp di, PROGRAM_BASE + 4
        jb .bss_done
        cmp word [di - 2], BSS_MAGIC
        jne .bss_done
        mov cx, [di - 4]                ; CX = BSS byte count
        test cx, cx
        jz .bss_done
        xor ax, ax
        cld
        rep stosb
        .bss_done:
        pop di
        pop cx
        pop ax
        ret

        shell_sp dw 0
        SHELL_ERROR db `Shell not found\n\0`
        SHELL_NAME db `bin/shell\0`
        WELCOME db `Welcome to BBoeOS!\nVersion 0.7.0 (2026/04/23)\n\0`
