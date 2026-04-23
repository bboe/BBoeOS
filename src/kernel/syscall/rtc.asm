        .rtc_datetime:
        call rtc_read_epoch     ; AX = epoch_lo, DX = epoch_hi
        mov [bp+14], ax
        mov [bp+10], dx
        jmp .iret_done

        .rtc_sleep:
        ;; Busy-wait for CX milliseconds via the native PIT tick counter.
        call rtc_sleep_ms
        jmp .iret_done

        .rtc_uptime:
        call uptime_seconds
        mov [bp+14], ax         ; return seconds in AX
        jmp .iret_done
