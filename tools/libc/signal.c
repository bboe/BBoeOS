#include <signal.h>

#include "include/errno.h"
#include "include/limits.h"
#include "include/unistd.h"

unsigned int alarm(unsigned int seconds) {
    /* POSIX: seconds = 0 cancels.  Returns prior remaining seconds
     * (rounded up to whole seconds — POSIX doesn't promise sub-second
     * precision and our internal ms is finer than the API).
     *
     * Clamp seconds before the *1000 so the multiply can't overflow
     * (UINT_MAX / 1000 ≈ 4 294 967 — about 49.7 days, well past any
     * sensible alarm).  Without the clamp a caller asking for e.g.
     * one year of seconds would silently arm a much shorter alarm. */
    if (seconds > UINT_MAX / 1000u) {
        seconds = UINT_MAX / 1000u;
    }
    unsigned int prev_ms = alarm_ms(seconds * 1000u, 0u);
    return (prev_ms + 999u) / 1000u;
}

unsigned int alarm_ms(unsigned int delay_ms, unsigned int interval_ms) {
    unsigned int eax_out;
    __asm__ volatile (
        "mov %[delay], %%ebx\n\t"
        "mov %[interval], %%ecx\n\t"
        "mov $0x30, %%ah\n\t"            /* SYS_RTC_ALARM */
        "int $0x30\n\t"
        : "=a"(eax_out)
        : [delay]"g"(delay_ms),
          [interval]"g"(interval_ms)
        : "ebx", "ecx");
    return eax_out;
}

sighandler_t signal(int signum, sighandler_t handler) {
    unsigned int eax_out;
    unsigned int cf;
    __asm__ volatile (
        "mov %[handler], %%ecx\n\t"
        "mov %[signum], %%ebx\n\t"
        "mov $0xF5, %%ah\n\t"            /* SYS_SYS_SIGNAL */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [signum]"g"((unsigned int)signum),
          [handler]"g"((unsigned int)handler)
        : "ebx", "ecx");
    if (cf & 1) {
        errno = EINVAL;
        return SIG_ERR;
    }
    return (sighandler_t)eax_out;
}
