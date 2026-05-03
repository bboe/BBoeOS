#ifndef BBOEOS_LIBC_ERRNO_H
#define BBOEOS_LIBC_ERRNO_H

extern int errno;

/* Match Linux numeric values for the small set we actually map.
 * BBoeOS kernel returns CF=1 with AL=ERROR_*; the syscall stubs in
 * syscall.c translate ERROR_* to the matching E* constant here. */
#define EPERM    1
#define ENOENT   2
#define EIO      5
#define EBADF    9
#define ENOMEM  12
#define EACCES  13
#define EFAULT  14
#define EEXIST  17
#define ENOTDIR 20
#define EISDIR  21
#define EINVAL  22
#define ENOSPC  28
#define ESPIPE  29
#define ENOSYS  38

#endif
