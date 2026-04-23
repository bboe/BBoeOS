;;; ------------------------------------------------------------------------
;;; rtc.asm — CMOS RTC reads, PIT-driven tick counter, millisecond sleep.
;;;
;;; Replaces BIOS INT 1Ah and INT 15h AH=86h that the syscall layer
;;; relied on:
;;;     INT 1Ah AH=04h (date) → rtc_read_date   (CH=cent, CL=yr, DH=mo, DL=dy)
;;;     INT 1Ah AH=02h (time) → rtc_read_time   (CH=hr, CL=min, DH=sec)
;;;     INT 1Ah AH=00h (ticks)→ rtc_tick_read   (EAX = ticks since boot)
;;;     INT 15h AH=86h (sleep)→ rtc_sleep_ms    (CX = milliseconds)
;;;
;;; A tiny IRQ 0 handler replaces the BIOS one so the counter keeps
;;; advancing after the pmode switch lands.  rtc_tick_init reprograms the
;;; PIT to 100 Hz (10 ms/tick) instead of the BIOS default ~18.2 Hz — gives
;;; 10 ms sleep granularity while leaving ≥10 ms of headroom before a CLI
;;; section starts losing ticks.
;;; ------------------------------------------------------------------------

        CMOS_CENTURY            equ 32h
        CMOS_DATA               equ 71h
        CMOS_DAY                equ 07h
        CMOS_HOURS              equ 04h
        CMOS_INDEX              equ 70h
        CMOS_MINUTES            equ 02h
        CMOS_MONTH              equ 08h
        CMOS_SECONDS            equ 00h
        CMOS_STATUS_A           equ 0Ah
        CMOS_UPDATE_IN_PROGRESS equ 80h
        CMOS_YEAR               equ 09h

        IVT_IRQ0_OFFSET         equ 8*4
        PIC1_CMD                equ 20h
        PIC_EOI                 equ 20h

        PIT_CHANNEL0            equ 40h
        PIT_COMMAND             equ 43h
        PIT_DIVISOR             equ 11932       ; 1193182 / 11932 ≈ 99.998 Hz
        PIT_MODE2_LOHI_CH0      equ 00110100b   ; ch0, lo/hi access, mode 2, binary

        MS_PER_TICK             equ 10
        TICKS_PER_SECOND        equ 100

rtc_read:
        ;; Input: AL = CMOS register index.  Output: AL = register value.
        ;; Clobbers nothing else.
        out CMOS_INDEX, al
        in al, CMOS_DATA
        ret

rtc_read_date:
        ;; Output: CH = century BCD, CL = year BCD,
        ;;         DH = month BCD,   DL = day BCD.
        ;; Clobbers AX.
        call rtc_wait_steady
        mov al, CMOS_CENTURY
        call rtc_read
        mov ch, al
        mov al, CMOS_YEAR
        call rtc_read
        mov cl, al
        mov al, CMOS_MONTH
        call rtc_read
        mov dh, al
        mov al, CMOS_DAY
        call rtc_read
        mov dl, al
        ret

rtc_read_time:
        ;; Output: CH = hours BCD, CL = minutes BCD, DH = seconds BCD.
        ;; Clobbers AX.
        call rtc_wait_steady
        mov al, CMOS_HOURS
        call rtc_read
        mov ch, al
        mov al, CMOS_MINUTES
        call rtc_read
        mov cl, al
        mov al, CMOS_SECONDS
        call rtc_read
        mov dh, al
        ret

rtc_sleep_ms:
        ;; Input: CX = milliseconds.  Busy-waits at least CX ms.
        ;; Preserves all registers.  10 ms granularity (one PIT tick).
        ;; Syscall handlers enter with IF=0 (INT clears it), so we must
        ;; sti here — otherwise IRQ 0 never fires during the wait and the
        ;; tick counter doesn't advance.  pushf/popf around the body keeps
        ;; the caller's IF intact either way.
        pushf
        push eax
        push ebx
        push ecx
        push edx
        movzx eax, cx
        add eax, MS_PER_TICK - 1 ; round up to whole ticks
        xor edx, edx
        mov ebx, MS_PER_TICK
        div ebx                 ; EAX = ticks, minimum 0
        test eax, eax
        jnz .have_ticks
        mov eax, 1              ; always wait at least one tick
        .have_ticks:
        mov ebx, eax
        sti
        call rtc_tick_read      ; EAX = now
        add ebx, eax            ; EBX = target
        .wait:
        call rtc_tick_read
        cmp eax, ebx
        jb .wait
        pop edx
        pop ecx
        pop ebx
        pop eax
        popf
        ret

rtc_tick_init:
        ;; Reprogram the PIT to 100 Hz, install our own IRQ 0 handler
        ;; into the real-mode IVT, and zero the tick counter.  Call once,
        ;; early in stage 2 boot.
        cli
        push ax
        push es
        mov al, PIT_MODE2_LOHI_CH0
        out PIT_COMMAND, al
        mov ax, PIT_DIVISOR
        out PIT_CHANNEL0, al    ; lo byte
        mov al, ah
        out PIT_CHANNEL0, al    ; hi byte
        xor ax, ax
        mov es, ax
        mov word [es:IVT_IRQ0_OFFSET], rtc_tick_irq0
        mov word [es:IVT_IRQ0_OFFSET + 2], cs
        mov dword [system_ticks], 0
        pop es
        pop ax
        sti
        ret

rtc_tick_irq0:
        ;; IRQ 0 handler.  Increment the tick counter, EOI, iret.  Force
        ;; DS=0 so the tick write lands in segment 0 even if the
        ;; interrupted code held a non-zero DS (e.g. vga_scroll_up).
        push eax
        push ds
        xor ax, ax
        mov ds, ax
        inc dword [system_ticks]
        mov al, PIC_EOI
        out PIC1_CMD, al
        pop ds
        pop eax
        iret

rtc_tick_read:
        ;; Output: EAX = monotonic tick counter.  Preserves everything
        ;; else.  CLI-bracketed so the 32-bit read is atomic vs IRQ 0.
        pushf
        cli
        mov eax, [system_ticks]
        popf
        ret

rtc_wait_steady:
        ;; Spin until the CMOS is not in an update cycle (UIP bit clear).
        ;; Gives us the ~244 µs window in which all time-of-day registers
        ;; are guaranteed stable.  Clobbers AX.
        .wait:
        mov al, CMOS_STATUS_A
        out CMOS_INDEX, al
        in al, CMOS_DATA
        test al, CMOS_UPDATE_IN_PROGRESS
        jnz .wait
        ret

        system_ticks dd 0
