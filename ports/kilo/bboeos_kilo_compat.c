/* bboeos compatibility shims for the kilo port.
 *
 * Implements the handful of POSIX libc entry points kilo expects that
 * bboeos's libbboeos does not provide.  The bboeos fd-0 console is
 * already raw and un-echoed, so the termios entry points are no-ops
 * that merely succeed; window sizing comes from kilo's ANSI cursor-query
 * fallback (see ports/kilo/include/sys/ioctl.h).
 */

#include <termios.h>
#include <time.h>
#include <unistd.h>

/* ftruncate: bboeos files are rewritten whole on save (open + O_TRUNC),
 * so there is nothing to shorten after the fact.  Succeed silently. */
int ftruncate(int fd, long length) {
    (void)fd;
    (void)length;
    return 0;
}

/* isatty: fd 0/1/2 are always the console in bboeos userland. */
int isatty(int fd) {
    return fd == 0 || fd == 1 || fd == 2;
}

/* tcgetattr / tcsetattr: the console has no mode to read or change, so
 * report a zeroed mode and accept any mode without acting on it. */
int tcgetattr(int fd, struct termios *termios_p) {
    (void)fd;
    if (termios_p) {
        char *bytes = (char *)termios_p;
        for (unsigned int i = 0; i < sizeof(*termios_p); i++) {
            bytes[i] = 0;
        }
    }
    return 0;
}

int tcsetattr(int fd, int optional_actions, const struct termios *termios_p) {
    (void)fd;
    (void)optional_actions;
    (void)termios_p;
    return 0;
}

/* time: kilo only needs a monotonic seconds counter to age the status
 * message, so derive it from the kernel uptime. */
time_t time(time_t *t) {
    time_t seconds = (time_t)(uptime_ms() / 1000u);
    if (t) {
        *t = seconds;
    }
    return seconds;
}
