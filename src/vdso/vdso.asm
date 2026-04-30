;;; ------------------------------------------------------------------------
;;; vdso.asm — user-space code blob containing FUNCTION_TABLE + shared_*
;;; helpers.  Assembled separately and embedded into the kernel binary.
;;; The kernel copies this 4 KB blob to physical 0x00010000 at boot so
;;; user programs can call FUNCTION_DIE / FUNCTION_PRINT_STRING / etc.
;;; via the addresses baked into constants.asm.
;;;
;;; All helpers are CPL=3 (no privileged instructions); they reach the
;;; kernel only via INT 30h syscalls.  Per-call scratch state lives on
;;; the user's stack — there is no vDSO data page in this milestone.
;;;
;;; Layout:
;;;   0x00010000  FUNCTION_TABLE (14 × 5-byte jmp slots = 70 bytes)
;;;   0x00010046  shared_die / shared_exit / shared_get_character / ...
;;;   0x00010XXX  print_datetime_month_lengths (read-only constant)
;;;   end of file (~1.1 KB actual content; the kernel maps the blob as
;;;   a user code page so unused tail bytes within the page are
;;;   irrelevant)
;;;
;;; Each per-program PD aliases the shared vDSO frame at user-virt
;;; 0x00010000 with the AVL[0] PTE_SHARED bit so
;;; `address_space_destroy` skips frame_free on it.
;;; ------------------------------------------------------------------------

        org 0x00010000
        bits 32

        %include "constants.asm"

;;; -----------------------------------------------------------------------
;;; FUNCTION_TABLE — 14 × 5-byte `jmp strict near` slots at offset 0.
;;; Order and address strides MUST match the FUNCTION_* constants in
;;; constants.asm, which programs `call` / `jmp` directly.
;;; -----------------------------------------------------------------------

function_table:
        jmp strict near shared_die              ; FUNCTION_DIE              (+0)
        jmp strict near shared_exit             ; FUNCTION_EXIT             (+5)
        jmp strict near shared_get_character    ; FUNCTION_GET_CHARACTER   (+10)
        jmp strict near shared_parse_argv       ; FUNCTION_PARSE_ARGV      (+15)
        jmp strict near shared_print_byte_decimal ; FUNCTION_PRINT_BYTE_DECIMAL (+20)
        jmp strict near shared_print_character  ; FUNCTION_PRINT_CHARACTER (+25)
        jmp strict near shared_print_datetime   ; FUNCTION_PRINT_DATETIME  (+30)
        jmp strict near shared_print_decimal    ; FUNCTION_PRINT_DECIMAL   (+35)
        jmp strict near shared_print_hex        ; FUNCTION_PRINT_HEX       (+40)
        jmp strict near shared_print_ip         ; FUNCTION_PRINT_IP        (+45)
        jmp strict near shared_print_mac        ; FUNCTION_PRINT_MAC       (+50)
        jmp strict near shared_print_string     ; FUNCTION_PRINT_STRING    (+55)
        jmp strict near shared_printf           ; FUNCTION_PRINTF          (+60)
        jmp strict near shared_write_stdout     ; FUNCTION_WRITE_STDOUT    (+65)

;;; -----------------------------------------------------------------------
;;; Helper bodies — ported from src/lib/proc.asm and src/lib/print.asm.
;;; All per-call scratch state (the byte transit for char I/O, printf's
;;; pad/width flags, print_datetime's intermediate fields) lives on the
;;; user stack.  No global vDSO data.
;;; -----------------------------------------------------------------------

shared_die:
        ;; SI = message, CX = length.  Writes to stdout, falls into shared_exit.
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        ;; Fall through.

shared_exit:
        mov ah, SYS_SYS_EXIT
        int 30h

shared_get_character:
        ;; Read one byte from stdin via SYS_IO_READ.  Returns the byte
        ;; zero-extended in EAX.  Stack layout: 4 bytes of scratch
        ;; below the saved registers; ESP holds the byte after int 30h.
        push ebx
        push ecx
        push edi
        sub esp, 4                              ; 1-byte transit (4 for stack alignment)
        mov bx, STDIN
        mov edi, esp
        mov ecx, 1
        mov ah, SYS_IO_READ
        int 30h
        movzx eax, byte [esp]
        add esp, 4
        pop edi
        pop ecx
        pop ebx
        ret

shared_parse_argv:
        ;; Split [EXEC_ARG] (kernel-side; unchanged this milestone) into
        ;; an argv-style array of dword pointers.
        ;; Input:  EDI = buffer for argv pointers
        ;; Output: ECX = argc
        ;; Clobbers: EAX, ESI (and EDI advances past the populated slots)
        xor ecx, ecx
        mov esi, [EXEC_ARG]
        test esi, esi
        jz .parse_argv_done
        .parse_argv_scan:
        cmp byte [esi], ' '
        jne .parse_argv_check
        inc esi
        jmp .parse_argv_scan
        .parse_argv_check:
        cmp byte [esi], 0
        je .parse_argv_done
        mov [edi], esi
        add edi, 4
        inc ecx
        .parse_argv_end:
        cmp byte [esi], 0
        je .parse_argv_done
        cmp byte [esi], ' '
        je .parse_argv_term
        inc esi
        jmp .parse_argv_end
        .parse_argv_term:
        mov byte [esi], 0
        inc esi
        jmp .parse_argv_scan
        .parse_argv_done:
        ret

shared_print_byte_decimal:
        ;; AL = byte: print 1-3 decimal digits, no leading zeros.
        push eax
        push ebx
        push ecx
        xor ah, ah
        xor ebx, ebx
        mov cl, 10
        .div_loop:
        div cl
        push eax
        inc ebx
        test al, al
        jz .print_digits
        xor ah, ah
        jmp .div_loop
        .print_digits:
        pop eax
        mov al, ah
        add al, '0'
        call shared_print_character
        dec ebx
        jnz .print_digits
        pop ecx
        pop ebx
        pop eax
        ret

shared_print_character:
        ;; AL = byte to write to stdout.  Preserves all registers.
        ;; Stack layout: 4-byte scratch above saved registers holds the
        ;; byte that SYS_IO_WRITE reads at ESI.
        push eax
        push ebx
        push ecx
        push esi
        sub esp, 4                              ; 1-byte transit (4 for stack alignment)
        mov [esp], al
        mov esi, esp
        mov ecx, 1
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        add esp, 4
        pop esi
        pop ecx
        pop ebx
        pop eax
        ret

shared_print_datetime:
        ;; DX:AX = unsigned seconds since 1970-01-01 00:00:00 UTC.
        ;; Prints YYYY-MM-DD HH:MM:SS (no trailing newline).
        ;;
        ;; Stack frame (below saved EBP):
        ;;   [ebp - 4]  print_datetime_days     (4 bytes; dword-aligned)
        ;;   [ebp - 6]  print_datetime_year     (2 bytes)
        ;;   [ebp - 7]  print_datetime_month    (1 byte)
        ;;   [ebp - 8]  print_datetime_hours    (1 byte)
        ;;   [ebp - 9]  print_datetime_minutes  (1 byte)
        ;;   [ebp - 10] print_datetime_seconds  (1 byte)
        push ebp
        mov ebp, esp
        sub esp, 12                             ; 10 bytes of state, rounded up to 12
        push eax
        push ebx
        push ecx
        push edx
        push esi

        ;; Combine DX:AX into a single 32-bit EAX.
        movzx ebx, ax
        movzx edx, dx
        shl edx, 16
        or ebx, edx
        mov eax, ebx

        mov ecx, 86400
        xor edx, edx
        div ecx
        mov [ebp - 4], eax                      ; print_datetime_days
        mov eax, edx

        mov ecx, 3600
        xor edx, edx
        div ecx
        mov [ebp - 8], al                       ; print_datetime_hours
        mov eax, edx

        mov ecx, 60
        xor edx, edx
        div ecx
        mov [ebp - 9], al                       ; print_datetime_minutes
        mov [ebp - 10], dl                      ; print_datetime_seconds

        ;; Walk years from 1970, peeling off 365 or 366 days each time.
        mov ebx, 1970
        .print_datetime_year_loop:
        mov eax, ebx
        call .print_datetime_is_leap
        jz .print_datetime_year_leap
        mov ecx, 365
        jmp .print_datetime_year_have_len
        .print_datetime_year_leap:
        mov ecx, 366
        .print_datetime_year_have_len:
        cmp [ebp - 4], ecx
        jb .print_datetime_year_done
        sub [ebp - 4], ecx
        inc ebx
        jmp .print_datetime_year_loop
        .print_datetime_year_done:
        mov [ebp - 6], bx                       ; print_datetime_year

        ;; Walk months within the year.
        mov ecx, 1
        .print_datetime_month_loop:
        mov ebx, ecx
        dec ebx
        shl ebx, 1
        movzx eax, word [print_datetime_month_lengths + ebx]
        cmp ecx, 2
        jne .print_datetime_month_len_ready
        push eax
        movzx eax, word [ebp - 6]
        call .print_datetime_is_leap
        pop eax
        jnz .print_datetime_month_len_ready
        inc eax
        .print_datetime_month_len_ready:
        cmp [ebp - 4], eax
        jb .print_datetime_month_done
        sub [ebp - 4], eax
        inc ecx
        jmp .print_datetime_month_loop
        .print_datetime_month_done:
        mov [ebp - 7], cl                       ; print_datetime_month

        ;; Emit YYYY-MM-DD HH:MM:SS.  print_datetime_days holds
        ;; (day-of-month - 1) at this point — no separate day field
        ;; needed; just inc and pass to shared_print_decimal.
        movzx eax, word [ebp - 6]               ; print_datetime_year
        call .print_datetime_print_4digit
        mov al, '-'
        call shared_print_character
        mov al, [ebp - 7]                       ; print_datetime_month
        call shared_print_decimal
        mov al, '-'
        call shared_print_character
        mov eax, [ebp - 4]                      ; print_datetime_days
        inc eax
        call shared_print_decimal
        mov al, ' '
        call shared_print_character
        mov al, [ebp - 8]                       ; print_datetime_hours
        call shared_print_decimal
        mov al, ':'
        call shared_print_character
        mov al, [ebp - 9]                       ; print_datetime_minutes
        call shared_print_decimal
        mov al, ':'
        call shared_print_character
        mov al, [ebp - 10]                      ; print_datetime_seconds
        call shared_print_decimal

        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        mov esp, ebp
        pop ebp
        ret

        .print_datetime_print_4digit:
        ;; EAX = value 0..9999.  Print 4 zero-padded decimal digits.
        push ebx
        push edx
        xor edx, edx
        mov ebx, 1000
        div ebx
        add al, '0'
        call shared_print_character
        mov eax, edx
        xor edx, edx
        mov ebx, 100
        div ebx
        add al, '0'
        call shared_print_character
        mov eax, edx
        xor edx, edx
        mov ebx, 10
        div ebx
        add al, '0'
        call shared_print_character
        mov eax, edx
        add al, '0'
        call shared_print_character
        pop edx
        pop ebx
        ret

        .print_datetime_is_leap:
        ;; EAX = year.  Returns ZF=1 if leap, ZF=0 otherwise.
        push ecx
        push eax
        xor edx, edx
        mov ecx, 4
        div ecx
        test edx, edx
        jnz .print_datetime_leap_no
        pop eax
        push eax
        xor edx, edx
        mov ecx, 100
        div ecx
        test edx, edx
        jnz .print_datetime_leap_yes
        pop eax
        push eax
        xor edx, edx
        mov ecx, 400
        div ecx
        test edx, edx
        jnz .print_datetime_leap_no
        .print_datetime_leap_yes:
        pop eax
        pop ecx
        xor eax, eax
        ret
        .print_datetime_leap_no:
        pop eax
        pop ecx
        or eax, 1
        ret

shared_print_decimal:
        ;; AL = byte 0..99: print 2 zero-padded decimal digits.
        aam
        xchg al, ah
        push eax
        add al, '0'
        call shared_print_character
        pop eax
        mov al, ah
        add al, '0'
        call shared_print_character
        ret

shared_print_hex:
        ;; AL = byte: print 2 uppercase hex digits.
        push eax
        shr al, 4
        call .nibble
        pop eax
        push eax
        and al, 0Fh
        call .nibble
        pop eax
        ret
        .nibble:
        cmp al, 10
        jb .digit
        add al, 'A' - 10
        jmp .hex_print
        .digit:
        add al, '0'
        .hex_print:
        call shared_print_character
        ret

shared_print_ip:
        ;; ESI = pointer to 4-byte IP.  Print as dotted decimal.
        push eax
        push ecx
        mov ecx, 4
        .ip_loop:
        lodsb
        call shared_print_byte_decimal
        dec ecx
        jz .ip_done
        mov al, '.'
        call shared_print_character
        jmp .ip_loop
        .ip_done:
        pop ecx
        pop eax
        ret

shared_print_mac:
        ;; ESI = pointer to 6-byte MAC.  Print as XX:XX:XX:XX:XX:XX.
        push eax
        push ecx
        mov ecx, 6
        .mac_loop:
        lodsb
        call shared_print_hex
        dec ecx
        jz .mac_done
        mov al, ':'
        call shared_print_character
        jmp .mac_loop
        .mac_done:
        pop ecx
        pop eax
        ret

shared_print_string:
        ;; DI = null-terminated string.  Length via repne scasb, then
        ;; shared_write_stdout.
        mov esi, edi
        xor al, al
        mov ecx, 0xFFFFFFFF
        cld
        repne scasb
        mov ecx, edi
        sub ecx, esi
        dec ecx
        call shared_write_stdout
        ret

shared_printf:
        ;; Minimal printf: cdecl, args 4 bytes each under --bits 32.
        ;;
        ;; Stack frame (below saved EBP):
        ;;   [ebp - 1]  printf_pad    (1 byte)
        ;;   [ebp - 2]  printf_width  (1 byte)
        push ebp
        mov ebp, esp
        sub esp, 4                              ; 2 bytes of state, rounded up to 4
        push esi
        push edi
        mov esi, [ebp+8]
        mov edi, 12
        cld
        .loop:
        lodsb
        test al, al
        jz .done
        cmp al, '%'
        je .format
        call shared_print_character
        jmp .loop

        .format:
        mov byte [ebp - 2], 0                   ; printf_width = 0
        mov byte [ebp - 1], ' '                 ; printf_pad = ' '
        lodsb
        cmp al, '0'
        jne .after_flag
        mov byte [ebp - 1], '0'                 ; printf_pad = '0'
        lodsb
        .after_flag:
        .width_loop:
        cmp al, '0'
        jb .spec
        cmp al, '9'
        ja .spec
        sub al, '0'
        push eax
        movzx eax, byte [ebp - 2]               ; printf_width
        mov dl, 10
        mul dl
        mov [ebp - 2], al                       ; printf_width = printf_width * 10
        pop eax
        add [ebp - 2], al                       ; printf_width += digit
        lodsb
        jmp .width_loop

        .spec:
        cmp al, 'c'
        je .fmt_c
        cmp al, 'd'
        je .fmt_d
        cmp al, 'u'
        je .fmt_u
        cmp al, 'x'
        je .fmt_x
        cmp al, 's'
        je .fmt_s
        cmp al, '%'
        je .fmt_percent
        call shared_print_character
        jmp .loop

        .fmt_c:
        mov eax, [ebp+edi]
        add edi, 4
        call shared_print_character
        jmp .loop

        .fmt_d:
        .fmt_u:
        mov eax, [ebp+edi]
        add edi, 4
        call .print_uint32
        jmp .loop

        .fmt_x:
        mov eax, [ebp+edi]
        add edi, 4
        call .print_hex_padded
        jmp .loop

        .fmt_s:
        push esi
        mov esi, [ebp+edi]
        add edi, 4
        push edi
        mov edi, esi
        xor al, al
        mov ecx, 0xFFFFFFFF
        repne scasb
        mov ecx, edi
        sub ecx, esi
        dec ecx
        pop edi
        call shared_write_stdout
        pop esi
        jmp .loop

        .fmt_percent:
        mov al, '%'
        call shared_print_character
        jmp .loop

        .done:
        pop edi
        pop esi
        mov esp, ebp
        pop ebp
        ret

        .print_uint32:
        xor ecx, ecx
        mov ebx, 10
        .udiv:
        xor edx, edx
        div ebx
        push edx
        inc ecx
        test eax, eax
        jnz .udiv
        push ecx
        .upad:
        cmp cl, [ebp - 2]                       ; printf_width
        jae .pad_done
        mov al, [ebp - 1]                       ; printf_pad
        call shared_print_character
        inc cl
        jmp .upad
        .pad_done:
        pop ecx
        .uprint:
        pop eax
        add al, '0'
        call shared_print_character
        dec ecx
        jnz .uprint
        ret

        .print_hex_padded:
        cmp byte [ebp - 2], 2                   ; printf_width
        jae .hskip_default
        mov byte [ebp - 2], 2
        .hskip_default:
        mov cl, 2
        .hpad:
        cmp cl, [ebp - 2]                       ; printf_width
        jae .hprint
        push eax
        mov al, [ebp - 1]                       ; printf_pad
        call shared_print_character
        pop eax
        inc cl
        jmp .hpad
        .hprint:
        push eax
        shr al, 4
        call .nibble
        pop eax
        and al, 0Fh
        call .nibble
        ret
        .nibble:
        cmp al, 10
        jb .digit
        add al, 'A' - 10
        jmp .nibble_print
        .digit:
        add al, '0'
        .nibble_print:
        call shared_print_character
        ret

shared_write_stdout:
        ;; ESI = buffer, ECX = byte count.  Writes to stdout.
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        ret

;;; -----------------------------------------------------------------------
;;; Read-only data — lives in the code page (R-X user; reads from
;;; executable pages are fine on x86 without NX).
;;; -----------------------------------------------------------------------

        align 2
print_datetime_month_lengths:
        dw 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
