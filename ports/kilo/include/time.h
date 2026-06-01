#ifndef BBOEOS_PORTS_KILO_TIME_H
#define BBOEOS_PORTS_KILO_TIME_H
/* Minimal <time.h> shim for the kilo port.
 *
 * kilo uses time(NULL) only to age the status-message line ("hide it
 * after 5 seconds"), so it needs nothing more than a monotonic seconds
 * counter.  Our impl (ports/kilo/bboeos_kilo_compat.c) derives it from
 * the kernel uptime, which is fine for relative timing.
 */

#include <sys/types.h>

typedef long time_t;

time_t time(time_t *t);

#endif
