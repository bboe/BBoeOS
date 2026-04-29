        ;; ------------------------------------------------------------
        ;; Real-time-clock syscalls.  Returns that overflow AX (DX:AX
        ;; pairs) get written explicitly into the saved EDX/ECX slots
        ;; so the user sees the same values after iretd.
        ;; ------------------------------------------------------------

        .rtc_datetime:
        ;; Returns DX:AX = unsigned epoch seconds (UTC), valid through
        ;; 2106-02-07.  CF clear (never errors).
        call rtc_read_epoch
        mov [esp + SYSCALL_SAVED_EDX], dx
        clc
        jmp .iret_cf

        .rtc_millis:
        ;; Returns DX:AX = milliseconds since boot.  Wraps at 2^32 ms
        ;; (~49.7 days).  CF clear.
        call rtc_tick_read
        imul eax, MS_PER_TICK
        mov edx, eax
        shr edx, 16
        mov [esp + SYSCALL_SAVED_EDX], dx
        clc
        jmp .iret_cf

        .rtc_sleep:
        ;; CX = milliseconds.  rtc_sleep_ms preserves all registers; CF
        ;; clear.
        call rtc_sleep_ms
        clc
        jmp .iret_cf

        .rtc_uptime:
        ;; Returns AX = seconds since boot.  CF clear.
        call rtc_tick_read
        xor edx, edx
        mov ecx, TICKS_PER_SECOND
        div ecx
        clc
        jmp .iret_cf
