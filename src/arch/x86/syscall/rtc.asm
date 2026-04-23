        .rtc_datetime:
        call rtc_read_epoch     ; AX = epoch_lo, DX = epoch_hi
        mov [bp+14], ax
        mov [bp+10], dx
        jmp .iret_done

        .rtc_millis:
        ;; DX:AX = milliseconds since boot (ticks × MS_PER_TICK).  Wraps at
        ;; 2^32 ms ≈ 49.7 days — longer than any realistic BBoeOS uptime.
        call rtc_tick_read      ; EAX = ticks
        imul eax, MS_PER_TICK   ; EAX = ms
        mov [bp+14], ax         ; AX slot = low 16 bits
        shr eax, 16
        mov [bp+10], ax         ; DX slot = high 16 bits
        jmp .iret_done

        .rtc_sleep:
        ;; Busy-wait for CX milliseconds via the native PIT tick counter.
        call rtc_sleep_ms
        jmp .iret_done

        .rtc_uptime:
        call uptime_seconds
        mov [bp+14], ax         ; return seconds in AX
        jmp .iret_done
