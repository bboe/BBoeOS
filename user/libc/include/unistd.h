#ifndef BBOEOS_LIBC_UNISTD_H
#define BBOEOS_LIBC_UNISTD_H
#include <stddef.h>
#include <sys/types.h>

/* Match kernel/include/constants.asm — the kernel ignores any flag bit it
 * doesn't recognise, so the previous (0x40) value silently turned every
 * libc-side O_CREAT into a no-op.  Until now nothing in userland passed
 * O_CREAT through libc; chocolate-doom's MUS-to-MID temp-file dance is
 * the first caller that needs it. */
#define O_CREAT 0x10
#define O_RDONLY 0
#define O_RDWR 2
#define O_TRUNC 0x20
#define O_WRONLY 1
#define SEEK_CUR 1
#define SEEK_END 2
#define SEEK_SET 0

void _exit(int status) __attribute__((noreturn));
unsigned int alarm(unsigned int seconds);
unsigned int alarm_ms(unsigned int delay_ms, unsigned int interval_ms);
int brk(void *addr);
int close(int fd);
int ioctl(int fd, int cmd, unsigned int ecx_arg, unsigned int edx_arg);
off_t lseek(int fd, off_t offset,
            int whence); /* stub: returns -1, sets errno=ESPIPE */
int open(const char *path, int flags, ...);
ssize_t read(int fd, void *buf, size_t count);
void *sbrk(ptrdiff_t increment);
/* sleep_ms: BBoeOS extension wrapping SYS_RTC_SLEEP.  Busy-waits at
 * least *ms* milliseconds.  Returns early (without setting errno) if
 * a pending signal short-circuited the kernel sleep — callers that
 * care should check the wall clock via uptime_ms or treat as a
 * cooperative-interrupt point. */
void sleep_ms(unsigned int ms);
/* uptime_ms: BBoeOS extension wrapping SYS_RTC_MILLIS.  Returns
 * monotonic milliseconds since boot; wraps at 2^32 ms (~49.7 days). */
unsigned int uptime_ms(void);
/* video_map: BBoeOS extension wrapping SYS_VIDEO_MAP.  Maps the
 * mode-13h framebuffer into the program's PD and returns its user-
 * virt address (the fixed 0xB8000000), or NULL on PT-allocation
 * failure (errno = ENOMEM). */
void *video_map(void);
ssize_t write(int fd, const void *buf, size_t count);

#endif
