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
        call ps2_init           ; mask BIOS IRQ 1 before anyone reads keys
        cmp byte [boot_disk], 80h
        jae .post_fdc
        call fdc_init           ; reset, motor, recalibrate drive A:
        .post_fdc:
        call fd_init
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

%include "ansi.asm"
%include "ata.asm"
%include "fd.asm"
%include "fdc.asm"
%include "io.asm"
%include "net.asm"
%include "ps2.asm"
%include "rtc.asm"
%include "shared.asm"
%include "syscall.asm"
%include "system.asm"
%include "vga.asm"
