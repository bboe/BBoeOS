#include <errno.h>
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
    return 0;
}

off_t lseek(int fd, off_t offset, int whence) {
    (void)fd; (void)offset; (void)whence;
    errno = ESPIPE;
    return (off_t)-1;
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

ssize_t write(int fd, const void *buf, size_t count) {
    unsigned int eax_out, cf;
    __asm__ volatile (
        "mov %[buf], %%esi\n\t"
        "mov %[len], %%ecx\n\t"
        "mov %[fd], %%bx\n\t"
        "mov $0x15, %%ah\n\t"           /* SYS_IO_WRITE */
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
