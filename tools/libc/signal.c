#include <signal.h>

#include "include/errno.h"

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
