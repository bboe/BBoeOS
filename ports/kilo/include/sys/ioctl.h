#ifndef BBOEOS_PORTS_KILO_SYS_IOCTL_H
#define BBOEOS_PORTS_KILO_SYS_IOCTL_H
/* Minimal sys/ioctl.h shim for the kilo port.
 *
 * kilo's getWindowSize() first tries POSIX `ioctl(1, TIOCGWINSZ, &ws)`
 * and, on failure, falls back to querying the terminal with the
 * `ESC [999C ESC [999B` + `ESC [6n` cursor dance — which the bboeos
 * ANSI console fully supports.  bboeos's ioctl() is variadic (see
 * <unistd.h>), so kilo's 3-argument call binds directly with no shim
 * macro.  TIOCGWINSZ masks to a console command the kernel does not
 * implement, so the call returns -1 and kilo takes the ANSI fallback.
 */

#include <unistd.h>

struct winsize {
    unsigned short ws_row;
    unsigned short ws_col;
    unsigned short ws_xpixel;
    unsigned short ws_ypixel;
};

#define TIOCGWINSZ 0x5413

#endif
