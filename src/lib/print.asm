;;; ---------------------------------------------------------------------
;;; print.asm — pmode user-side print helpers, jumped to via FUNCTION_TABLE
;;; from cc.py-emitted code.
;;;
;;; Helpers stay in their original alphabetical positions.  The ones cc.py
;;; needs for the current program set (`shared_print_character`,
;;; `shared_print_string`, `shared_printf`, `shared_write_stdout`) carry
;;; pmode bodies.  The rest keep their 16-bit bodies wrapped in `%if 0`
;;; so a future port can diff against the original in place; the jump
;;; table routes those slots through `shared_not_impl` (halt) until the
;;; body lands.
;;; ---------------------------------------------------------------------

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_byte_decimal:
        ;; Print AL as 1-3 digit decimal (no leading zeros)
        push ax
        push bx
        push cx
        xor ah, ah
        xor bx, bx             ; Digit count
        mov cl, 10
        .div_loop:
        div cl                 ; AL = quotient, AH = remainder
        push ax
        inc bx
        test al, al
        jz .print_digits
        xor ah, ah
        jmp .div_loop
        .print_digits:
        pop ax
        mov al, ah
        add al, '0'
        call shared_print_character
        dec bx
        jnz .print_digits
        pop cx
        pop bx
        pop ax
        ret
%endif

shared_print_character:
        ;; AL = byte to write to stdout.  Preserves all registers.
        push eax
        push ebx
        push ecx
        push esi
        mov [SECTOR_BUFFER], al
        mov esi, SECTOR_BUFFER
        mov ecx, 1
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        pop esi
        pop ecx
        pop ebx
        pop eax
        ret

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_datetime:
        ;; Input: DX:AX = unsigned seconds since 1970-01-01 00:00:00 UTC.
        ;; Prints: YYYY-MM-DD HH:MM:SS (no trailing newline).
        ;; Full Gregorian leap rule. Valid through year 2106.
        push eax
        push ebx
        push ecx
        push edx
        push si

        ;; Combine DX:AX into EAX (zero-extend AX, shift DX into high 16).
        movzx ebx, ax
        movzx edx, dx
        shl edx, 16
        or ebx, edx
        mov eax, ebx

        mov ecx, 86400
        xor edx, edx
        div ecx                 ; EAX = day count, EDX = seconds within day
        mov [.pd_days], eax
        mov eax, edx

        mov ecx, 3600
        xor edx, edx
        div ecx                 ; EAX = hours, EDX = seconds within hour
        mov [.pd_hours], al
        mov eax, edx

        mov ecx, 60
        xor edx, edx
        div ecx                 ; EAX = minutes, EDX = seconds
        mov [.pd_minutes], al
        mov [.pd_seconds], dl

        ;; Walk years from 1970, peeling off 365 or 366 days each time.
        mov bx, 1970
        .pd_year_loop:
        mov ax, bx
        call .pd_is_leap
        jz .pd_year_leap
        mov ecx, 365
        jmp .pd_year_have_len
        .pd_year_leap:
        mov ecx, 366
        .pd_year_have_len:
        cmp [.pd_days], ecx
        jb .pd_year_done
        sub [.pd_days], ecx
        inc bx
        jmp .pd_year_loop
        .pd_year_done:
        mov [.pd_year], bx

        ;; Walk months within the year.
        mov cx, 1               ; CX = candidate month (1..12)
        .pd_month_loop:
        mov bx, cx
        dec bx
        shl bx, 1
        movzx eax, word [.pd_month_lengths + bx]
        cmp cx, 2
        jne .pd_month_len_ready
        push ax
        mov ax, [.pd_year]
        call .pd_is_leap
        pop ax
        jnz .pd_month_len_ready
        movzx eax, ax
        inc eax                 ; February in leap year = 29
        .pd_month_len_ready:
        cmp [.pd_days], eax
        jb .pd_month_done
        sub [.pd_days], eax
        inc cx
        jmp .pd_month_loop
        .pd_month_done:
        mov [.pd_month], cl
        mov eax, [.pd_days]
        inc eax
        mov [.pd_day], al

        ;; Emit YYYY-MM-DD HH:MM:SS
        mov ax, [.pd_year]
        call .pd_print_4digit
        mov al, '-'
        call shared_print_character
        mov al, [.pd_month]
        call shared_print_decimal
        mov al, '-'
        call shared_print_character
        mov al, [.pd_day]
        call shared_print_decimal
        mov al, ' '
        call shared_print_character
        mov al, [.pd_hours]
        call shared_print_decimal
        mov al, ':'
        call shared_print_character
        mov al, [.pd_minutes]
        call shared_print_decimal
        mov al, ':'
        call shared_print_character
        mov al, [.pd_seconds]
        call shared_print_decimal

        pop si
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

        .pd_print_4digit:
        ;; AX = value 0..9999. Print 4 zero-padded decimal digits.
        push bx
        push dx
        xor dx, dx
        mov bx, 1000
        div bx
        add al, '0'
        call shared_print_character
        mov ax, dx
        xor dx, dx
        mov bx, 100
        div bx
        add al, '0'
        call shared_print_character
        mov ax, dx
        xor dx, dx
        mov bx, 10
        div bx
        add al, '0'
        call shared_print_character
        mov ax, dx
        add al, '0'
        call shared_print_character
        pop dx
        pop bx
        ret

        .pd_is_leap:
        ;; AX = year. Returns ZF=1 if leap, ZF=0 otherwise. Clobbers AX, DX.
        push cx
        push ax
        xor dx, dx
        mov cx, 4
        div cx
        test dx, dx
        jnz .pd_leap_no
        pop ax
        push ax
        xor dx, dx
        mov cx, 100
        div cx
        test dx, dx
        jnz .pd_leap_yes
        pop ax
        push ax
        xor dx, dx
        mov cx, 400
        div cx
        test dx, dx
        jnz .pd_leap_no
        .pd_leap_yes:
        pop ax
        pop cx
        xor ax, ax              ; ZF=1
        ret
        .pd_leap_no:
        pop ax
        pop cx
        or ax, 1                ; ZF=0
        ret

        .pd_month_lengths:
        dw 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
        .pd_days:    dd 0
        .pd_year:    dw 0
        .pd_month:   db 0
        .pd_day:     db 0
        .pd_hours:   db 0
        .pd_minutes: db 0
        .pd_seconds: db 0
%endif

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_decimal:
        ;; Print AL as 2 zero-padded decimal digits
        aam                     ; AH = AL/10, AL = AL%10
        xchg al, ah             ; AL = tens, AH = ones
        push ax
        add al, '0'
        call shared_print_character
        pop ax
        mov al, ah
        add al, '0'
        call shared_print_character
        ret
%endif

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_hex:
        ;; Print AL as two uppercase hex digits
        push ax
        shr al, 4
        call .nibble
        pop ax
        push ax
        and al, 0Fh
        call .nibble
        pop ax
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
%endif

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_ip:
        ;; Print 4-byte IP address as dotted decimal
        ;; Input: SI = pointer to 4-byte IP
        push ax
        push cx
        mov cx, 4
        .ip_loop:
        lodsb
        call shared_print_byte_decimal
        dec cx
        jz .ip_done
        mov al, '.'
        call shared_print_character
        jmp .ip_loop
        .ip_done:
        pop cx
        pop ax
        ret
%endif

%if 0   ; not yet ported — pmode body lands when cc.py emits this helper
shared_print_mac:
        ;; Print a 6-byte MAC address as XX:XX:XX:XX:XX:XX
        ;; Input: SI = pointer to 6-byte MAC address
        push ax
        push cx
        mov cx, 6
        .mac_loop:
        lodsb
        call shared_print_hex
        dec cx
        jz .mac_done
        mov al, ':'
        call shared_print_character
        jmp .mac_loop
        .mac_done:
        pop cx
        pop ax
        ret
%endif

shared_print_string:
        ;; DI = null-terminated string.  Computes length via repne scasb,
        ;; then writes via shared_write_stdout.  Clobbers EAX, EBX, ECX,
        ;; ESI.
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
        ;; Minimal printf: cdecl, args are 4 bytes each under --bits 32.
        ;; Stack: [ebp+8] = format string, [ebp+12] = first arg, …
        ;; Supports: %c %d %u %x %s %%, optional zero-pad flag and width.
        ;; Format: %[0][width]<type>
        push ebp
        mov ebp, esp
        push esi
        push edi
        mov esi, [ebp+8]                ; format string pointer
        mov edi, 12                     ; stack offset for next arg
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
        ;; Reset width / pad for this conversion.
        mov byte [printf_width], 0
        mov byte [printf_pad], ' '
        lodsb
        cmp al, '0'
        jne .after_flag
        mov byte [printf_pad], '0'
        lodsb
        .after_flag:
        .width_loop:
        cmp al, '0'
        jb .spec
        cmp al, '9'
        ja .spec
        sub al, '0'
        push eax
        movzx eax, byte [printf_width]
        mov dl, 10
        mul dl                          ; AX = width * 10
        mov [printf_width], al
        pop eax
        add [printf_width], al
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
        ;; Unknown specifier: print literal.
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
        ;; Strlen via repne scasb on a copy of ESI.
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
        pop ebp
        ret

        .print_uint32:
        ;; Print EAX as unsigned decimal, padded to [printf_width] with
        ;; [printf_pad].  Clobbers EAX, EBX, ECX, EDX.
        xor ecx, ecx                    ; digit count
        mov ebx, 10
        .udiv:
        xor edx, edx
        div ebx                         ; EAX = quotient, EDX = remainder
        push edx
        inc ecx
        test eax, eax
        jnz .udiv
        ;; Pad to width, using CL as scratch counter.
        push ecx
        .upad:
        cmp cl, [printf_width]
        jae .pad_done
        mov al, [printf_pad]
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
        ;; Print AL as hex, padded to [printf_width] with [printf_pad].
        ;; Default width for %x is 2.  Clobbers EAX, ECX.
        cmp byte [printf_width], 2
        jae .hskip_default
        mov byte [printf_width], 2
        .hskip_default:
        mov cl, 2
        .hpad:
        cmp cl, [printf_width]
        jae .hprint
        push eax
        mov al, [printf_pad]
        call shared_print_character
        pop eax
        inc cl
        jmp .hpad
        .hprint:
        ;; Two hex digits from AL.
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
        ;; ESI = buffer, ECX = byte count.  Writes to stdout.  Preserves
        ;; nothing the SYS_IO_WRITE handler doesn't already preserve.
        mov bx, STDOUT
        mov ah, SYS_IO_WRITE
        int 30h
        ret

        printf_pad    db 0
        printf_width  db 0
