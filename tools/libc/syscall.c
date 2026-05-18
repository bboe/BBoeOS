#include <errno.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <syscalls.h>
#include <unistd.h>

/* All syscalls follow the BBoeOS convention: AH = syscall number,
 * arg registers per docs/syscalls.md, CF=1 on error with AL holding
 * an ERROR_* code.  Wrappers translate ERROR_* -> errno.
 *
 * ERROR_* values come from src/include/constants.asm (alphabetical-by-name,
 * values renumbered to match):
 *   01h ERROR_DIRECTORY_FULL  -> ENOSPC
 *   02h ERROR_EXISTS          -> EEXIST
 *   03h ERROR_FAULT           -> EFAULT
 *   04h ERROR_INTERRUPTED     -> EINTR
 *   05h ERROR_INVALID         -> EINVAL
 *   0Ah ERROR_IS_DIRECTORY    -> EISDIR
 *   0Bh ERROR_NOT_DIRECTORY   -> ENOTDIR
 *   06h ERROR_NOT_EMPTY       -> ENOTEMPTY (mapped to EACCES; not in our errno.h)
 *   07h ERROR_NOT_EXECUTE     -> EACCES
 *   08h ERROR_NOT_FOUND       -> ENOENT
 *   09h ERROR_PROTECTED       -> EACCES
 */
static unsigned int _current_break = 0;

static int _errno_from_al(int al) {
    switch (al) {
    case ERROR_DIRECTORY_FULL:
        return ENOSPC;
    case ERROR_EXISTS:
        return EEXIST;
    case ERROR_FAULT:
        return EFAULT;
    case ERROR_INTERRUPTED:
        return EINTR;
    case ERROR_INVALID:
        return EINVAL;
    case ERROR_IS_DIRECTORY:
        return EISDIR;
    case ERROR_NOT_DIRECTORY:
        return ENOTDIR;
    case ERROR_NOT_EMPTY:
        return EACCES; /* no ENOTEMPTY in our errno.h */
    case ERROR_NOT_EXECUTE:
        return EACCES;
    case ERROR_NOT_FOUND:
        return ENOENT;
    case ERROR_PROTECTED:
        return EACCES;
    default:
        return EIO;
    }
}

void _exit(int status) {
    __asm__ volatile("mov %[s], %%al\n\t"
                     "mov $" SYSNUM_STR(SYS_SYS_EXIT) ", %%ah\n\t"
                                                      "int $0x30\n\t"
                     :
                     : [s] "g"((unsigned char)status)
                     : "ax");
    while (1) {
    } /* unreachable */
}

int brk(void *addr) {
    unsigned int eax_out;
    __asm__ volatile("mov %[a], %%ebx\n\t"
                     "mov $" SYSNUM_STR(SYS_SYS_BREAK) ", %%ah\n\t"
                                                       "int $0x30\n\t"
                     : "=a"(eax_out)
                     : [a] "g"((unsigned int)addr)
                     : "ebx");
    if (eax_out != (unsigned int)addr) {
        errno = ENOMEM;
        return -1;
    }
    return 0;
}

int close(int fd) {
    unsigned int eax_out, cf;
    __asm__ volatile("mov %[fd], %%bx\n\t"
                     "mov $" SYSNUM_STR(SYS_IO_CLOSE) ", %%ah\n\t"
                                                      "int $0x30\n\t"
                                                      "setc %b[cf]\n\t"
                     : "=a"(eax_out), [cf] "=&q"(cf)
                     : [fd] "g"((unsigned short)fd)
                     : "ebx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return -1;
    }
    return 0;
}

int getdents(int fd, void *buffer, int count) {
    /* SYS_IO_GETDENTS: BX=fd, DI=buffer, CX=count; returns AX=bytes
     * written (0 at EOF), CF=1 with AL=ERROR_* on failure
     * (e.g. ERROR_NOT_DIRECTORY when fd is a regular file). */
    unsigned int eax_out, cf;
    __asm__ volatile(
        "mov %[buffer], %%edi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $" SYSNUM_STR(SYS_IO_GETDENTS) ", %%ah\n\t"
                                            "int $0x30\n\t"
                                            "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf] "=&q"(cf)
        : [buffer] "g"((unsigned int)buffer), [len] "g"((unsigned int)count),
          [fd] "g"((unsigned short)fd)
        : "edi", "ecx", "ebx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return -1;
    }
    return (int)eax_out;
}

int gettimeofday(struct timeval *tv, struct timezone *tz) {
    /* Returns the same monotonic ms-since-boot value via SYS_RTC_MILLIS
     * for both fields — Doom only cares about deltas for frame timing,
     * not absolute wall-clock.  tz is ignored (POSIX-compliant). */
    (void)tz;
    if (tv == 0)
        return 0;
    unsigned int total_ms;
    __asm__ volatile("mov $" SYSNUM_STR(SYS_RTC_MILLIS) ", %%ah\n\t"
                                                        "int $0x30\n\t"
                     : "=a"(total_ms));
    tv->tv_sec = total_ms / 1000;
    tv->tv_usec = (total_ms % 1000) * 1000;
    return 0;
}

/* Generic ioctl: AH=SYS_IO_IOCTL, BX=fd, AL=cmd, ECX/EDX=args.
 * Bind inputs directly to the kernel's expected registers to keep the
 * extended-asm constraint count low — the alternative ("g" everywhere
 * + clobbers) trips clang's "inline assembly requires more registers
 * than available" under -O2 because EAX/EBX/ECX/EDX are all spoken
 * for as both inputs and outputs/clobbers. */
int ioctl(int fd, int cmd, unsigned int ecx_arg, unsigned int edx_arg) {
    /* Pack AH=SYS_IO_IOCTL, AL=cmd into the EAX seed.  cmd is masked
     * to 8 bits so the low byte ends up in AL after the int. */
    unsigned int eax_in_out =
        (unsigned int)((SYS_IO_IOCTL << 8) | (cmd & 0xFF));
    unsigned char cf;
    __asm__ volatile("int $0x30\n\t"
                     "setc %[cf]\n\t"
                     : "+a"(eax_in_out), [cf] "=&qm"(cf), "+b"(fd),
                       "+c"(ecx_arg), "+d"(edx_arg));
    if (cf & 1) {
        errno = _errno_from_al(eax_in_out & 0xFF);
        return -1;
    }
    /* Return the kernel's full EAX so commands that report data wider
     * than 16 bits get their value through.  Notably:
     *
     *   CONSOLE_IOCTL_TRY_GETC      — one byte in AX (low 8)
     *   CONSOLE_IOCTL_TRY_GET_EVENT — (pressed << 16) | bbkey in EAX
     *
     * Truncating to AX here would clip the press flag of every BBKEY
     * event, which made Doom see release-only events and ignore
     * keypresses entirely.  Most ioctls return 0, so existing callers
     * that ignore the return are unaffected. */
    return (int)eax_in_out;
}

off_t lseek(int fd, off_t offset, int whence) {
    unsigned int eax_out, cf;
    __asm__ volatile(
        "mov %[fd], %%bx\n\t"
        "mov %[offset], %%ecx\n\t"
        "mov %[whence], %%al\n\t"
        "mov $" SYSNUM_STR(SYS_IO_SEEK) ", %%ah\n\t"
                                        "int $0x30\n\t"
                                        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf] "=&q"(cf)
        : [fd] "g"((unsigned short)fd), [offset] "g"((unsigned int)offset),
          [whence] "g"((unsigned char)whence)
        : "ebx", "ecx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return (off_t)-1;
    }
    return (off_t)eax_out;
}

int mkdir(const char *path, int mode) {
    /* Stub: would route to SYS_FS_MKDIR (01h) but Doom only calls
     * mkdir() to create save-game / config dirs we don't support
     * anyway.  Return failure so callers fall back to the cwd. */
    (void)path;
    (void)mode;
    return -1;
}

int open(const char *path, int flags, ...) {
    unsigned int eax_out, cf;
    __asm__ volatile(
        "mov %[path], %%esi\n\t"
        "mov %[flags], %%al\n\t"
        "xor %%dl, %%dl\n\t"
        "mov $" SYSNUM_STR(SYS_IO_OPEN) ", %%ah\n\t"
                                        "int $0x30\n\t"
                                        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf] "=&q"(cf)
        : [path] "g"((unsigned int)path), [flags] "g"((unsigned char)flags)
        : "esi", "edx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return -1;
    }
    return (int)(eax_out & 0xFFFF);
}

ssize_t read(int fd, void *buf, size_t count) {
    /* Kernel fd_read implementations write the full byte count to
     * EAX and the syscall dispatcher routes IO_READ through
     * .iret_cf_eax (no AX→EAX sign-extend), so a 70 KB request comes
     * back as 70 KB.  Pass count through unmodified and read all 32
     * bits of the kernel's return. */
    unsigned int eax_out, cf;
    __asm__ volatile(
        "mov %[buf], %%edi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $" SYSNUM_STR(SYS_IO_READ) ", %%ah\n\t"
                                        "int $0x30\n\t"
                                        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf] "=&q"(cf)
        : [buf] "g"((unsigned int)buf), [len] "g"((unsigned int)count),
          [fd] "g"((unsigned short)fd)
        : "edi", "ecx", "ebx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return -1;
    }
    return (ssize_t)eax_out;
}

void *sbrk(ptrdiff_t increment) {
    if (_current_break == 0) {
        unsigned int eax_out;
        __asm__ volatile("xor %%ebx, %%ebx\n\t"
                         "mov $" SYSNUM_STR(SYS_SYS_BREAK) ", %%ah\n\t"
                                                           "int $0x30\n\t"
                         : "=a"(eax_out)
                         :
                         : "ebx");
        _current_break = eax_out;
    }
    unsigned int requested = _current_break + (unsigned int)increment;
    if (brk((void *)requested) != 0)
        return (void *)-1;
    void *old = (void *)_current_break;
    _current_break = requested;
    return old;
}

void sleep_ms(unsigned int ms) {
    /* SYS_RTC_SLEEP busy-waits ms milliseconds in the kernel.  Returns
     * early with CF=1 if interrupted by a pending signal — we discard
     * that here; callers needing EINTR semantics should use the raw
     * syscall or check the clock themselves. */
    if (ms == 0)
        return;
    __asm__ volatile("mov %[ms], %%ecx\n\t"
                     "mov $" SYSNUM_STR(SYS_RTC_SLEEP) ", %%ah\n\t"
                                                       "int $0x30\n\t"
                     :
                     : [ms] "g"(ms)
                     : "ax", "ecx", "cc");
}

int stat(const char *path, struct stat *buf) {
    /* Stub: Doom uses stat() only for IWAD-search heuristics (probe a
     * few candidate paths).  Returning failure makes Doom fall through
     * to the explicit -iwad command-line path we hand it. */
    (void)path;
    (void)buf;
    return -1;
}

unsigned int uptime_ms(void) {
    unsigned int ms;
    __asm__ volatile("mov $" SYSNUM_STR(SYS_RTC_MILLIS) ", %%ah\n\t"
                                                        "int $0x30\n\t"
                     : "=a"(ms));
    return ms;
}

void *video_map(void) {
    /* SYS_VIDEO_MAP returns CF=0 with EAX = MODE13H_USER_VIRT (the
     * fixed 0xB8000000) on success; CF=1 with EAX = 0 on PT-allocation
     * failure.  NULL is unambiguous as a failure sentinel because the
     * success address is never 0; callers can `if (va == NULL)`. */
    unsigned int va;
    unsigned char cf;
    __asm__ volatile("mov $" SYSNUM_STR(SYS_VIDEO_MAP) ", %%ah\n\t"
                                                       "int $0x30\n\t"
                                                       "setc %[cf]\n\t"
                     : "=a"(va), [cf] "=&qm"(cf));
    if (cf & 1) {
        errno = ENOMEM;
        return NULL;
    }
    return (void *)va;
}

ssize_t write(int fd, const void *buf, size_t count) {
    /* Same 32-bit return shape as read: kernel fd_write writes EAX,
     * dispatcher uses .iret_cf_eax. */
    unsigned int eax_out, cf;
    __asm__ volatile(
        "mov %[buf], %%esi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $" SYSNUM_STR(SYS_IO_WRITE) ", %%ah\n\t"
                                         "int $0x30\n\t"
                                         "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf] "=&q"(cf)
        : [buf] "g"((unsigned int)buf), [len] "g"((unsigned int)count),
          [fd] "g"((unsigned short)fd)
        : "esi", "ecx", "ebx");
    if (cf & 1) {
        errno = _errno_from_al(eax_out & 0xFF);
        return -1;
    }
    return (ssize_t)eax_out;
}
