        [bits 32]
        org 0600h

%include "constants.asm"

;; Borrow otherwise-unused fixed regions instead of carrying static
;; storage inside the shell binary.  kill_buffer lives past the 1-byte
;; scratch slot that FUNCTION_GET_CHARACTER writes into on every
;; keypress; exec_path reuses the ARGV region (32 bytes), which is
;; free here because the shell never calls FUNCTION_PARSE_ARGV on its
;; own command line.  ``SECTOR_BUFFER`` (phys 0xF000) lived in the
;; live constants header until shell.c moved its kill buffer to BSS;
;; this archive snapshot keeps a private %assign for the historical
;; real-mode layout.
%assign SECTOR_BUFFER 0F000h
%assign exec_path   ARGV
%assign kill_buffer SECTOR_BUFFER + 4

main:
        cld
        ;; Open /dev/vga for ioctl-based video-mode switching.  Stash the
        ;; fd so Ctrl-L can reuse it without reopening on every keypress.
        mov esi, DEV_VGA
        mov al, O_WRONLY
        mov ah, SYS_IO_OPEN
        int 30h
        mov [vga_fd], eax
prompt:
        mov esi, PROMPT
        mov ecx, PROMPT_LENGTH
        call FUNCTION_WRITE_STDOUT

        call read_line
        test ecx, ecx
        jz prompt

        ;; Split command at first space
        mov esi, BUFFER
        mov dword [EXEC_ARG], 0
.find_space:
        lodsb
        cmp al, ' '
        je .found_space
        test al, al
        jnz .find_space
        jmp .split_done
.found_space:
        mov byte [esi-1], 0    ; Null-terminate command name
        mov [EXEC_ARG], esi    ; Point to argument
.split_done:
        ;; EDX = command name length including null terminator
        mov edx, esi
        sub edx, BUFFER

.dispatch:
        mov ebx, cmd_table
.loop:
        mov edi, [ebx]
        test edi, edi
        jz .not_found

        mov ecx, edx
        mov esi, BUFFER
        repe cmpsb
        jne .next

        call dword [ebx+4]
        jmp .output

.next:
        add ebx, 8             ; cmd_table entry stride: 4-byte name + 4-byte fn
        jmp .loop

.not_found:
        ;; Try to execute as external program by literal name
        mov esi, BUFFER
        mov ah, SYS_SYS_EXEC
        int 30h                 ; Does not return on success
        cmp al, ERROR_NOT_EXECUTE
        je .not_exec
        ;; Not found in root: retry inside bin/.  Write the "bin/"
        ;; prefix as a single dword: little-endian "n/ib" with bytes
        ;; 'b','i','n','/' lands as 0x2F6E6962.
        mov dword [exec_path], 0x2F6E6962
        mov esi, BUFFER
        mov edi, exec_path + 4 ; just past "bin/"
        mov ecx, DIRECTORY_NAME_LENGTH    ; name + null
        .copy_name:
        lodsb
        stosb
        test al, al
        jz .copy_done
        loop .copy_name
        .copy_done:
        mov byte [edi], 0      ; ensure null-termination
        mov esi, exec_path
        mov ah, SYS_SYS_EXEC
        int 30h                 ; Does not return on success
        cmp al, ERROR_NOT_EXECUTE
        je .not_exec
        mov esi, INVALID_COMMAND
        jmp .output
        .not_exec:
        mov esi, NOT_EXECUTABLE

.output:
        test esi, esi
        jz prompt
        mov edi, esi
        call FUNCTION_PRINT_STRING
        jmp prompt

;; Command handlers
;; Return: ESI = string to print, or ESI = 0 for no output

cmd_help:
        push ebx
        mov esi, HELP_PREFIX
        mov ecx, HELP_PREFIX_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov ebx, cmd_table
.help_loop:
        mov edi, [ebx]
        test edi, edi
        jz .help_end
        push ebx
        call FUNCTION_PRINT_STRING
        mov al, ' '
        call FUNCTION_PRINT_CHARACTER
        pop ebx
        add ebx, 8
        jmp .help_loop
.help_end:
        pop ebx
        mov al, `\n`
        call FUNCTION_PRINT_CHARACTER
        xor esi, esi
        ret

cmd_reboot:
        mov ah, SYS_SYS_REBOOT
        jmp syscall_null

cmd_shutdown:
        mov ah, SYS_SYS_SHUTDOWN
        int 30h
        mov esi, SHUTDOWN_FAIL
        ret

;; Line editor

read_line:
        push eax
        push ebx
        push edx
        mov ecx, BUFFER         ; Cursor position
        mov edx, BUFFER         ; End of buffer

        .read_char:
        call FUNCTION_GET_CHARACTER

        cmp al, 0               ; Extended key
        je .extended_key
        cmp al, 0E0h            ; Extended key (alternate)
        je .extended_key
        cmp al, 01h             ; Ctrl+A — beginning of line
        je .ctrl_a
        cmp al, 02h             ; Ctrl+B — cursor left
        je .cursor_left
        cmp al, 03h             ; Ctrl+C — cancel line
        je .ctrl_c
        cmp al, 04h             ; Ctrl+D — shutdown
        je .ctrl_d
        cmp al, 05h             ; Ctrl+E — end of line
        je .ctrl_e
        cmp al, 06h             ; Ctrl+F — cursor right
        je .cursor_right
        cmp al, `\b`            ; Backspace
        je .backspace
        cmp al, 7Fh             ; DEL (serial terminal backspace)
        je .backspace
        cmp al, 0Bh             ; Ctrl+K — kill to end of line
        je .ctrl_k
        cmp al, 0Ch             ; Ctrl+L — clear screen
        je .ctrl_l
        cmp al, `\r`            ; Enter
        je .end
        cmp al, 19h             ; Ctrl+Y — yank from kill buffer
        je .ctrl_y
        cmp al, 20h             ; Ignore other control characters
        jl .read_char

        call .insert_char       ; Insert character at cursor
        jnc .read_char
        call visual_bell
        jmp .read_char

        .extended_key:
        cmp ah, 4Bh             ; Left arrow
        je .cursor_left
        cmp ah, 4Dh             ; Right arrow
        je .cursor_right
        cmp ah, 53h             ; Delete
        je .delete
        jmp .read_char          ; Ignore other extended keys

        .cursor_left:
        cmp ecx, BUFFER
        je .read_char
        dec ecx
        mov ebx, 1
        call emit_cursor_back
        jmp .read_char

        .cursor_right:
        cmp ecx, edx
        je .read_char
        mov al, [ecx]
        call putc
        inc ecx
        jmp .read_char

        .backspace:
        cmp ecx, BUFFER
        je .read_char
        dec ecx
        mov ebx, 1
        call emit_cursor_back
        call .delete_at_cursor
        jmp .read_char

        .delete:
        cmp ecx, edx
        je .read_char
        call .delete_at_cursor
        jmp .read_char

        .ctrl_a:
        cmp ecx, BUFFER
        je .read_char
        mov ebx, ecx
        sub ebx, BUFFER
        call emit_cursor_back
        mov ecx, BUFFER
        jmp .read_char

        .ctrl_c:
        mov al, `\n`
        call putc
        mov ecx, BUFFER
        mov edx, BUFFER
        jmp .return

        .ctrl_d:
        mov ah, SYS_SYS_SHUTDOWN
        int 30h
        jmp .read_char          ; If shutdown fails, continue

        .ctrl_e:
        cmp ecx, edx
        je .read_char
        .ce_loop:
        mov al, [ecx]
        call putc
        inc ecx
        cmp ecx, edx
        jne .ce_loop
        jmp .read_char

        .ctrl_k:
        cmp ecx, edx
        je .read_char
        push esi
        push edi
        ;; Copy killed text to kill buffer
        mov esi, ecx
        mov edi, kill_buffer
        mov ebx, edx
        sub ebx, ecx
        cmp ebx, MAX_INPUT
        jbe .ck_save
        mov ebx, MAX_INPUT
        .ck_save:
        mov [kill_length], ebx
        .ck_copy:
        mov al, [esi]
        mov [edi], al
        inc esi
        inc edi
        dec ebx
        jnz .ck_copy
        ;; Erase killed text: print spaces, then cursor back
        mov ebx, edx
        sub ebx, ecx            ; Count of chars to erase
        push ebx                ; Save count for cursor_back
        mov esi, ebx
        .ck_erase:
        mov al, ' '
        call putc
        dec esi
        jnz .ck_erase
        pop ebx
        call emit_cursor_back
        mov edx, ecx            ; Truncate buffer at cursor
        pop edi
        pop esi
        jmp .read_char

        .ctrl_y:
        push esi
        mov esi, kill_buffer
        mov ebx, [kill_length]
        test ebx, ebx
        jz .cy_done
        .cy_loop:
        mov al, [esi]
        push ebx
        call .insert_char
        pop ebx
        jc .cy_full             ; Stop yanking if buffer full
        inc esi
        dec ebx
        jnz .cy_loop
        jmp .cy_done
        .cy_full:
        call visual_bell
        .cy_done:
        pop esi
        jmp .read_char

        .ctrl_l:
        mov ebx, [vga_fd]
        mov dl, VIDEO_MODE_TEXT_80x25
        mov al, VGA_IOCTL_MODE
        mov ah, SYS_IO_IOCTL
        int 30h
        mov ecx, BUFFER
        mov edx, BUFFER
        jmp .return

        .end:
        mov al, `\n`
        call putc
        .return:
        mov byte [edx], 0      ; Add null terminating character to buffer
        mov ecx, edx
        sub ecx, BUFFER        ; Store how many characters were read in ECX
        pop edx
        pop ebx
        pop eax
        ret

        ;; Insert char in AL at cursor, shift buffer right, redraw
        .insert_char:
        push ebx
        mov ebx, edx
        sub ebx, BUFFER
        cmp ebx, MAX_INPUT
        pop ebx
        jb .ic_ok
        stc                     ; Set carry flag to signal buffer full
        ret
        .ic_ok:
        push esi
        push eax
        mov esi, edx
        .ic_shift:
        cmp esi, ecx
        jle .ic_insert
        dec esi
        mov al, [esi]
        mov [esi+1], al
        jmp .ic_shift
        .ic_insert:
        pop eax
        mov [ecx], al
        inc edx
        ;; Print from cursor to end via putc
        mov esi, ecx
        .ic_print:
        cmp esi, edx
        jge .ic_repos
        mov al, [esi]
        call putc
        inc esi
        jmp .ic_print
        .ic_repos:
        inc ecx
        mov ebx, edx
        sub ebx, ecx
        call emit_cursor_back
        clc                     ; Clear carry flag to signal success
        pop esi
        ret

        ;; Delete char at cursor, shift buffer left, redraw
        .delete_at_cursor:
        push esi
        mov esi, ecx
        inc esi
        .dac_shift:
        cmp esi, edx
        jge .dac_redraw
        mov al, [esi]
        dec esi
        mov [esi], al
        inc esi
        inc esi
        jmp .dac_shift
        .dac_redraw:
        dec edx
        ;; Print from cursor to end, space to erase, then cursor back
        mov esi, ecx
        .dac_print:
        cmp esi, edx
        jge .dac_erase
        mov al, [esi]
        call putc
        inc esi
        jmp .dac_print
        .dac_erase:
        mov al, ' '
        call putc               ; Erase trailing character
        mov ebx, edx
        sub ebx, ecx
        inc ebx
        call emit_cursor_back
        pop esi
        ret

;; Utility functions

emit_cursor_back:
        ;; Emit ESC[nD sequence via putc
        ;; Input: EBX = count (0 = no-op)
        test ebx, ebx
        jz .ecb_done
        push eax
        mov al, 1Bh
        call putc
        mov al, '['
        call putc
        mov eax, ebx
        call .emit_decimal
        mov al, 'D'
        call putc
        pop eax
.ecb_done:
        ret

.emit_decimal:
        ;; Emit EAX as decimal digits via putc
        push ecx
        push edx
        xor ecx, ecx            ; Digit count
.ed_div:
        xor edx, edx
        mov ebx, 10
        div ebx                 ; EAX = quotient, EDX = remainder
        push edx                ; Push digit
        inc ecx
        test eax, eax
        jnz .ed_div
.ed_print:
        pop eax
        add al, '0'
        call putc
        loop .ed_print
        pop edx
        pop ecx
        ret

putc:
        ;; Print char in AL via kernel jump table
        jmp FUNCTION_PRINT_CHARACTER

syscall_null:
        int 30h
        xor esi, esi
        ret

visual_bell:
        push eax
        push ebx
        push ecx
        push edx
        push esi
        ;; Flash background red via SGR: \e[48;5;4m
        mov esi, BELL_RED
        mov ecx, BELL_SGR_LEN
        call FUNCTION_WRITE_STDOUT
        mov ecx, 50             ; 50 ms
        mov ah, SYS_RTC_SLEEP
        int 30h
        ;; Restore background black: \e[48;5;0m
        mov esi, BELL_BLACK
        mov ecx, BELL_SGR_LEN
        call FUNCTION_WRITE_STDOUT
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

BELL_BLACK   db `\e[48;5;0m`
BELL_RED     db `\e[48;5;4m`
BELL_SGR_LEN equ $ - BELL_RED

;; Command table — 4-byte name pointer + 4-byte fn pointer per entry.
cmd_table:
        dd .help,     cmd_help
        dd .reboot,   cmd_reboot
        dd .shutdown, cmd_shutdown
        dd 0
        .help     db `help\0`
        .reboot   db `reboot\0`
        .shutdown db `shutdown\0`

;; Strings
DEV_VGA       db `/dev/vga\0`
HELP_PREFIX   db `Commands: `
HELP_PREFIX_LENGTH equ $ - HELP_PREFIX
INVALID_COMMAND   db `unknown command\n\0`
NOT_EXECUTABLE      db `not executable\n\0`
PROMPT        db `$ `
PROMPT_LENGTH equ $ - PROMPT
SHUTDOWN_FAIL db `APM shutdown failed\n\0`

;; Variables
;; kill_buffer and exec_path live in the fixed scratch regions declared
;; as %assigns at the top of this file; no binary storage is reserved
;; for them here.
kill_length   dd 0
vga_fd        dd 0

