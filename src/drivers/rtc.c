// rtc.c — CMOS RTC reads, PIT-driven tick counter, millisecond sleep.
//
// Replaces BIOS INT 1Ah and INT 15h AH=86h that the syscall layer
// relied on:
//     INT 1Ah AH=04h (date) → rtc_read_date  (CH=cent, CL=yr, DH=mo, DL=dy)
//     INT 1Ah AH=02h (time) → rtc_read_time  (CH=hr,  CL=min, DH=sec)
//     INT 1Ah AH=00h (ticks)→ rtc_tick_read  (EAX = ticks since boot)
//     INT 15h AH=86h (sleep)→ rtc_sleep_ms   (CX  = milliseconds)
//
// The PIT is reprogrammed to 100 Hz (10 ms/tick) and the IRQ 0 handler
// (`pmode_irq0_handler` in entry.asm) is wired into the protected mode
// IDT during `protected_mode_entry`, replacing the BIOS default
// ~18.2 Hz tick.  Constants used by entry.asm and fdc.asm
// (PIT_*, MS_PER_TICK, TICKS_PER_SECOND, PIC_EOI) live in
// src/include/constants.asm so they survive the asm file's removal.
//
// CMOS register layout / port addresses inlined as bare integers
// (cc.py emits #define as %define which would clash with constants.asm
// equ values in the shared %include namespace):
//   CMOS_INDEX = 0x70, CMOS_DATA = 0x71
//   CMOS_SECONDS = 0x00, CMOS_MINUTES = 0x02, CMOS_HOURS = 0x04
//   CMOS_DAY = 0x07, CMOS_MONTH = 0x08, CMOS_YEAR = 0x09
//   CMOS_STATUS_A = 0x0A, CMOS_CENTURY = 0x32
//   CMOS_UPDATE_IN_PROGRESS = 0x80

uint8_t epoch_day;
uint8_t epoch_hours;
uint8_t epoch_minutes;
uint8_t epoch_month;
uint8_t epoch_seconds;
uint16_t epoch_year;
int rtc_month_days[12] = {
    0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334,
};
uint32_t system_ticks;

// `entry.asm`'s IRQ 0 handler does ``inc dword [system_ticks]`` and
// the post-flip init does ``mov dword [system_ticks], 0``.  cc.py
// emits the storage as ``_g_system_ticks``; alias the bare name so
// the asm consumer doesn't need to know about that.
asm("system_ticks equ _g_system_ticks");

// Forward declaration: rtc_read_epoch_impl (sorts after rtc_read_epoch
// alphabetically) is called from rtc_read_epoch's asm() body.
uint32_t rtc_read_epoch_impl()
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("esi")));

// Forward declaration: rtc_read_time_internal sorts after
// rtc_read_epoch_impl alphabetically and is called from its body.
void rtc_read_time_internal(int *cx __attribute__((out_register("cx"))),
                            int *dx __attribute__((out_register("dx"))));

// Convert one BCD byte to binary.  AL → AL.
uint8_t rtc_bcd_to_bin(uint8_t bcd __attribute__((in_register("ax")))) {
    return ((bcd >> 4) & 0x0F) * 10 + (bcd & 0x0F);
}

// Returns 1 if `year` is a Gregorian leap year, 0 otherwise.
int rtc_is_leap_year(int year) {
    if ((year & 3) != 0) { return 0; }       // not divisible by 4
    if ((year % 100) != 0) { return 1; }     // div by 4, not by 100 → leap
    if ((year % 400) != 0) { return 0; }     // div by 100, not by 400 → not leap
    return 1;
}

// Read one CMOS register.  AL = register index → AL = value.
uint8_t rtc_read(uint8_t reg __attribute__((in_register("ax")))) {
    kernel_outb(0x70, reg);
    return kernel_inb(0x71);
}

// rtc_read_date: returns CH=century, CL=year, DH=month, DL=day (all BCD).
// Internal helper that the C side calls; the asm-side ABI is unused.
void rtc_read_date_internal(int *cx __attribute__((out_register("cx"))),
                            int *dx __attribute__((out_register("dx"))));

asm("rtc_read_date_internal:\n"
    "    push eax\n"
    "    call rtc_wait_steady\n"
    "    mov al, 0x32\n"   // CMOS_CENTURY
    "    call rtc_read\n"
    "    mov ch, al\n"
    "    mov al, 0x09\n"   // CMOS_YEAR
    "    call rtc_read\n"
    "    mov cl, al\n"
    "    mov al, 0x08\n"   // CMOS_MONTH
    "    call rtc_read\n"
    "    mov dh, al\n"
    "    mov al, 0x07\n"   // CMOS_DAY
    "    call rtc_read\n"
    "    mov dl, al\n"
    "    pop eax\n"
    "    ret");

// rtc_read_epoch: returns DX:AX = unsigned epoch seconds since
// 1970-01-01 UTC, valid through 2106-02-07.  CF clear (never errors).
// The substantive C content lives in rtc_read_epoch_impl; the
// public symbol is a thin wrapper that splits the 32-bit EAX result
// into the asm-side DX:AX shape callers expect.
void rtc_read_epoch();

asm("rtc_read_epoch:\n"
    "    call rtc_read_epoch_impl\n"
    "    mov edx, eax\n"
    "    shr edx, 16\n"
    "    ret");

uint32_t rtc_read_epoch_impl()
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("esi")))
{
    int cx;
    int dx;
    int year;
    int days;
    int month_index;
    int seconds;

    rtc_read_date_internal(&cx, &dx);
    year = rtc_bcd_to_bin((cx >> 8) & 0xFF) * 100 + rtc_bcd_to_bin(cx & 0xFF);
    epoch_year = year;
    epoch_month = rtc_bcd_to_bin((dx >> 8) & 0xFF);
    epoch_day = rtc_bcd_to_bin(dx & 0xFF);

    rtc_read_time_internal(&cx, &dx);
    epoch_hours = rtc_bcd_to_bin((cx >> 8) & 0xFF);
    epoch_minutes = rtc_bcd_to_bin(cx & 0xFF);
    epoch_seconds = rtc_bcd_to_bin((dx >> 8) & 0xFF);

    // Days from 1970-01-01 to the current year's January 1.
    days = 0;
    year = 1970;
    while (year < epoch_year) {
        if (rtc_is_leap_year(year)) {
            days = days + 366;
        } else {
            days = days + 365;
        }
        year = year + 1;
    }

    // Days within the current year up to the start of `epoch_month`.
    month_index = epoch_month - 1;
    days = days + rtc_month_days[month_index];

    // After Feb in a leap year, add one extra day.
    if (epoch_month > 2) {
        if (rtc_is_leap_year(epoch_year)) {
            days = days + 1;
        }
    }

    days = days + epoch_day - 1;

    seconds = days * 86400;
    seconds = seconds + epoch_hours * 3600;
    seconds = seconds + epoch_minutes * 60;
    seconds = seconds + epoch_seconds;
    return seconds;
}

// rtc_read_time: returns CH=hours, CL=minutes, DH=seconds (BCD).
asm("rtc_read_time_internal:\n"
    "    push eax\n"
    "    call rtc_wait_steady\n"
    "    mov al, 0x04\n"   // CMOS_HOURS
    "    call rtc_read\n"
    "    mov ch, al\n"
    "    mov al, 0x02\n"   // CMOS_MINUTES
    "    call rtc_read\n"
    "    mov cl, al\n"
    "    mov al, 0x00\n"   // CMOS_SECONDS
    "    call rtc_read\n"
    "    mov dh, al\n"
    "    pop eax\n"
    "    ret");

// rtc_sleep_ms: CX = milliseconds.  Busy-waits at least CX ms.
// 10 ms granularity (one PIT tick).  Preserves all registers.
// Syscall handlers enter with IF=0 (INT clears it), so we sti
// inside — IRQ 0 must fire for the tick counter to advance.
// pushf/popf around the body keeps the caller's IF intact.
void rtc_sleep_ms(int ms __attribute__((in_register("cx"))));

asm("rtc_sleep_ms:\n"
    "    pushf\n"
    "    push eax\n"
    "    push ebx\n"
    "    push ecx\n"
    "    push edx\n"
    "    movzx eax, cx\n"
    "    add eax, 9\n"           // round up to whole ticks (MS_PER_TICK - 1)
    "    xor edx, edx\n"
    "    mov ebx, 10\n"          // MS_PER_TICK
    "    div ebx\n"
    "    test eax, eax\n"
    "    jnz .rsm_have_ticks\n"
    "    mov eax, 1\n"           // always wait at least one tick
    ".rsm_have_ticks:\n"
    "    mov ebx, eax\n"
    "    sti\n"
    "    call rtc_tick_read\n"
    "    add ebx, eax\n"
    ".rsm_wait:\n"
    "    call rtc_tick_read\n"
    "    cmp eax, ebx\n"
    "    jb .rsm_wait\n"
    "    pop edx\n"
    "    pop ecx\n"
    "    pop ebx\n"
    "    pop eax\n"
    "    popf\n"
    "    ret");

// rtc_tick_read: returns EAX = monotonic tick counter (32-bit).
// The asm ABI says preserves all other registers.  Uses cli/popf
// bracketing so the 32-bit read is atomic vs IRQ 0 increments.
// Implemented as inline asm because cc.py's natural codegen for a
// global read would clobber EFLAGS in the prologue/epilogue and
// not give us the atomicity guarantee the asm contract documents.
asm("rtc_tick_read:\n"
    "    pushf\n"
    "    cli\n"
    "    mov eax, [_g_system_ticks]\n"
    "    popf\n"
    "    ret");

// Spin until the CMOS UIP bit clears — gives the ~244 µs window in
// which all time-of-day registers are guaranteed stable.
void rtc_wait_steady() {
    while (1) {
        kernel_outb(0x70, 0x0A);
        if ((kernel_inb(0x71) & 0x80) == 0) {
            return;
        }
    }
}

// uptime_seconds: AX = elapsed seconds since boot.  Preserves ECX, EDX.
// Computes EAX = system_ticks / TICKS_PER_SECOND (= 100).
void uptime_seconds();

asm("uptime_seconds:\n"
    "    push ecx\n"
    "    push edx\n"
    "    call rtc_tick_read\n"
    "    xor edx, edx\n"
    "    mov ecx, 100\n"
    "    div ecx\n"
    "    pop edx\n"
    "    pop ecx\n"
    "    ret");
