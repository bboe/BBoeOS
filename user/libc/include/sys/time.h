#ifndef BBOEOS_LIBC_SYS_TIME_H
#define BBOEOS_LIBC_SYS_TIME_H
/* Minimal sys/time.h — Doom calls gettimeofday() for timing.  Our
 * impl reads RTC + TSC via the kernel and returns Unix-epoch-ish
 * seconds + microseconds (good enough for frame timing; not for
 * absolute wall-clock). */
#include <sys/types.h>

struct timeval {
    long tv_sec;
    long tv_usec;
};

struct timezone {
    int tz_minuteswest;
    int tz_dsttime;
};

int gettimeofday(struct timeval *tv, struct timezone *tz);

#endif
