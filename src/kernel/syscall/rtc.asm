        .rtc_datetime:
        ;; Returns DX:AX = unsigned seconds since 1970-01-01 00:00:00 UTC.
        ;; Gregorian leap rule. Valid through 2106 (32-bit seconds).
        push ebx
        push ecx
        push esi

        call rtc_read_date      ; CH=century BCD, CL=year BCD, DH=month BCD, DL=day BCD
        mov al, ch
        call .bcd_to_bin
        movzx si, al            ; SI = century
        imul si, si, 100        ; SI = century * 100
        mov al, cl
        call .bcd_to_bin
        movzx bx, al
        add si, bx              ; SI = full year
        mov [epoch_year], si
        mov al, dh
        call .bcd_to_bin
        mov [epoch_month], al
        mov al, dl
        call .bcd_to_bin
        mov [epoch_day], al

        call rtc_read_time      ; CH=hours BCD, CL=minutes BCD, DH=seconds BCD
        mov al, ch
        call .bcd_to_bin
        mov [epoch_hours], al
        mov al, cl
        call .bcd_to_bin
        mov [epoch_minutes], al
        mov al, dh
        call .bcd_to_bin
        mov [epoch_seconds], al

        ;; Days from 1970-01-01 to the first of epoch_year.
        xor esi, esi            ; ESI = day accumulator
        mov cx, 1970
        .rtc_year_loop:
        cmp cx, [epoch_year]
        jae .rtc_year_done
        mov ax, cx
        call .is_leap_year
        jz .rtc_year_leap
        add esi, 365
        jmp .rtc_year_next
        .rtc_year_leap:
        add esi, 366
        .rtc_year_next:
        inc cx
        jmp .rtc_year_loop
        .rtc_year_done:

        ;; Add cumulative days before the first of epoch_month.
        movzx bx, byte [epoch_month]
        dec bx
        shl bx, 1
        movzx eax, word [.month_days + bx]
        add esi, eax

        ;; If current year is leap and month > 2, add one extra day for Feb 29.
        cmp byte [epoch_month], 2
        jbe .rtc_skip_leap_adj
        mov ax, [epoch_year]
        call .is_leap_year
        jnz .rtc_skip_leap_adj
        inc esi
        .rtc_skip_leap_adj:

        ;; Add day-of-month minus 1.
        movzx eax, byte [epoch_day]
        dec eax
        add esi, eax

        ;; seconds = days*86400 + h*3600 + m*60 + s
        mov eax, esi
        mov ecx, 86400
        mul ecx                 ; EDX:EAX = EAX * ECX (EDX discarded; fits in 32 bits through 2106)
        movzx ebx, byte [epoch_hours]
        imul ebx, ebx, 3600
        add eax, ebx
        movzx ebx, byte [epoch_minutes]
        imul ebx, ebx, 60
        add eax, ebx
        movzx ebx, byte [epoch_seconds]
        add eax, ebx

        ;; Return DX:AX = EAX
        pop esi
        pop ecx
        pop ebx
        mov edx, eax
        shr edx, 16             ; DX = high 16
        mov [bp+14], ax         ; AX = low 16 of epoch seconds
        mov [bp+10], dx         ; DX = high 16 of epoch seconds
        jmp .iret_done

        .bcd_to_bin:
        ;; AL (BCD) -> AL (binary). Clobbers AX.
        push cx
        mov cl, al
        shr al, 4
        mov ch, 10
        mul ch                  ; AX = high_nibble * 10
        and cl, 0Fh
        add al, cl
        pop cx
        ret

        .is_leap_year:
        ;; AX = year. Returns ZF=1 if leap, ZF=0 otherwise.
        ;; Preserves CX, EAX beyond low word. Clobbers AX, DX.
        push cx
        push ax
        xor dx, dx
        mov cx, 4
        div cx                  ; DX = year % 4
        test dx, dx
        jnz .leap_no_pop
        pop ax
        push ax
        xor dx, dx
        mov cx, 100
        div cx                  ; DX = year % 100
        test dx, dx
        jnz .leap_yes_pop       ; div 4, not div 100 -> leap
        pop ax
        push ax
        xor dx, dx
        mov cx, 400
        div cx                  ; DX = year % 400
        test dx, dx
        jnz .leap_no_pop        ; div 100, not div 400 -> not leap
        .leap_yes_pop:
        pop ax
        pop cx
        xor ax, ax              ; ZF=1
        ret
        .leap_no_pop:
        pop ax
        pop cx
        or ax, 1                ; ZF=0
        ret

        .month_days:
        dw 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334

        .rtc_sleep:
        ;; Busy-wait for CX milliseconds via the native PIT tick counter.
        call rtc_sleep_ms
        jmp .iret_done

        .rtc_uptime:
        call uptime_seconds
        mov [bp+14], ax         ; return seconds in AX
        jmp .iret_done
