#include <errno.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

/* All syscalls follow the BBoeOS convention: AH = syscall number,
 * arg registers per docs/syscalls.md, CF=1 on error with AL holding
 * an ERROR_* code.  Wrappers translate ERROR_* -> errno.
 *
 * ERROR_* values come from src/include/constants.asm:
 *   01h ERROR_DIRECTORY_FULL  -> ENOSPC
 *   02h ERROR_EXISTS          -> EEXIST
 *   03h ERROR_NOT_EXECUTE     -> EACCES
 *   04h ERROR_NOT_FOUND       -> ENOENT
 *   05h ERROR_PROTECTED       -> EACCES
 *   06h ERROR_NOT_EMPTY       -> ENOTEMPTY (mapped to EACCES; not in our errno.h)
 *   07h ERROR_FAULT           -> EFAULT
 */
static unsigned int _current_break = 0;

static int _errno_from_al(int al) {
    switch (al) {
        case 0x01: return ENOSPC;       /* ERROR_DIRECTORY_FULL */
        case 0x02: return EEXIST;       /* ERROR_EXISTS */
        case 0x03: return EACCES;       /* ERROR_NOT_EXECUTE */
        case 0x04: return ENOENT;       /* ERROR_NOT_FOUND */
        case 0x05: return EACCES;       /* ERROR_PROTECTED */
        case 0x06: return EACCES;       /* ERROR_NOT_EMPTY (no ENOTEMPTY in our errno.h) */
        case 0x07: return EFAULT;       /* ERROR_FAULT */
        default:   return EIO;
    }
}

void _exit(int status) {
    (void)status;
    __asm__ volatile ("mov $0xF2, %ah; int $0x30");        /* SYS_SYS_EXIT */
    while (1) {}    /* unreachable */
}

int brk(void *addr) {
    unsigned int eax_out;
    __asm__ volatile (
        "mov %[a], %%ebx\n\t"
        "mov $0xF0, %%ah\n\t"           /* SYS_SYS_BREAK */
        "int $0x30\n\t"
        : "=a"(eax_out)
        : [a]"g"((unsigned int)addr)
        : "ebx");
    if (eax_out != (unsigned int)addr) { errno = ENOMEM; return -1; }
    return 0;
}

int close(int fd) {
    unsigned int eax_out, cf;
    __asm__ volatile (
        "mov %[fd], %%bx\n\t"
        "mov $0x10, %%ah\n\t"           /* SYS_IO_CLOSE */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [fd]"g"((unsigned short)fd)
        : "ebx");
    if (cf & 1) { errno = _errno_from_al(eax_out & 0xFF); return -1; }
    return 0;
}

int gettimeofday(struct timeval *tv, struct timezone *tz) {
    /* Returns the same monotonic ms-since-boot value via SYS_RTC_MILLIS
     * for both fields — Doom only cares about deltas for frame timing,
     * not absolute wall-clock.  tz is ignored (POSIX-compliant). */
    (void)tz;
    if (tv == 0) return 0;
    unsigned int ms_lo, ms_hi;
    __asm__ volatile (
        "mov $0x31, %%ah\n\t"           /* SYS_RTC_MILLIS */
        "int $0x30\n\t"
        : "=a"(ms_lo), "=d"(ms_hi));
    /* DX:AX is ms; pack to a 32-bit ms count, then split into sec / usec. */
    unsigned int total_ms = (ms_hi << 16) | (ms_lo & 0xFFFF);
    tv->tv_sec  = total_ms / 1000;
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
    unsigned int eax_in_out = (unsigned int)(0x1200 | (cmd & 0xFF));    /* AH=12h, AL=cmd */
    unsigned char cf;
    __asm__ volatile (
        "int $0x30\n\t"
        "setc %[cf]\n\t"
        : "+a"(eax_in_out), [cf]"=&qm"(cf),
          "+b"(fd), "+c"(ecx_arg), "+d"(edx_arg));
    if (cf & 1) { errno = _errno_from_al(eax_in_out & 0xFF); return -1; }
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
    __asm__ volatile (
        "mov %[fd], %%bx\n\t"
        "mov %[offset], %%ecx\n\t"
        "mov %[whence], %%al\n\t"
        "mov $0x15, %%ah\n\t"           /* SYS_IO_SEEK */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [fd]"g"((unsigned short)fd),
          [offset]"g"((unsigned int)offset),
          [whence]"g"((unsigned char)whence)
        : "ebx", "ecx");
    if (cf & 1) { errno = _errno_from_al(eax_out & 0xFF); return (off_t)-1; }
    return (off_t)eax_out;
}

int mkdir(const char *path, int mode) {
    /* Stub: would route to SYS_FS_MKDIR (01h) but Doom only calls
     * mkdir() to create save-game / config dirs we don't support
     * anyway.  Return failure so callers fall back to the cwd. */
    (void)path; (void)mode;
    return -1;
}

int open(const char *path, int flags, ...) {
    unsigned int eax_out, cf;
    __asm__ volatile (
        "mov %[path], %%esi\n\t"
        "mov %[flags], %%al\n\t"
        "xor %%dl, %%dl\n\t"
        "mov $0x13, %%ah\n\t"           /* SYS_IO_OPEN */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [path]"g"((unsigned int)path), [flags]"g"((unsigned char)flags)
        : "esi", "edx");
    if (cf & 1) { errno = _errno_from_al(eax_out & 0xFF); return -1; }
    return (int)(eax_out & 0xFFFF);
}

ssize_t read(int fd, void *buf, size_t count) {
    /* Kernel SYS_IO_READ returns the byte count in AX (16 bits) — a
     * 70 KB request would actually advance the file position by 70 KB
     * but report (70 KB & 0xFFFF) = 4464 bytes back, fooling callers
     * into re-reading already-consumed data.  Cap each syscall at
     * 65535 so AX never wraps; POSIX permits short reads, so callers
     * that need more (fread, our wrapper) just loop. */
    if (count > 0xFFFFu) count = 0xFFFFu;
    unsigned int eax_out, cf;
    __asm__ volatile (
        "mov %[buf], %%edi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $0x14, %%ah\n\t"           /* SYS_IO_READ */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [buf]"g"((unsigned int)buf),
          [len]"g"((unsigned int)count),
          [fd] "g"((unsigned short)fd)
        : "edi", "ecx", "ebx");
    if (cf & 1) { errno = _errno_from_al(eax_out & 0xFF); return -1; }
    return (ssize_t)(eax_out & 0xFFFF);
}

void *sbrk(ptrdiff_t increment) {
    if (_current_break == 0) {
        unsigned int eax_out;
        __asm__ volatile (
            "xor %%ebx, %%ebx\n\t"
            "mov $0xF0, %%ah\n\t"           /* SYS_SYS_BREAK */
            "int $0x30\n\t"
            : "=a"(eax_out) : : "ebx");
        _current_break = eax_out;
    }
    unsigned int requested = _current_break + (unsigned int)increment;
    if (brk((void*)requested) != 0) return (void*)-1;
    void *old = (void*)_current_break;
    _current_break = requested;
    return old;
}

int stat(const char *path, struct stat *buf) {
    /* Stub: Doom uses stat() only for IWAD-search heuristics (probe a
     * few candidate paths).  Returning failure makes Doom fall through
     * to the explicit -iwad command-line path we hand it. */
    (void)path; (void)buf;
    return -1;
}

ssize_t write(int fd, const void *buf, size_t count) {
    /* Same 16-bit AX truncation as read — cap each syscall at 65535. */
    if (count > 0xFFFFu) count = 0xFFFFu;
    unsigned int eax_out, cf;
    __asm__ volatile (
        "mov %[buf], %%esi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $0x16, %%ah\n\t"           /* SYS_IO_WRITE */
        "int $0x30\n\t"
        "setc %b[cf]\n\t"
        : "=a"(eax_out), [cf]"=&q"(cf)
        : [buf]"g"((unsigned int)buf),
          [len]"g"((unsigned int)count),
          [fd] "g"((unsigned short)fd)
        : "esi", "ecx", "ebx");
    if (cf & 1) { errno = _errno_from_al(eax_out & 0xFF); return -1; }
    return (ssize_t)(eax_out & 0xFFFF);
}
