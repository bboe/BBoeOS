#ifndef BBOEOS_LIBC_SYS_TYPES_H
#define BBOEOS_LIBC_SYS_TYPES_H
#include <stddef.h>
typedef long off_t;
/* Signed counterpart of size_t — use ptrdiff_t to track the compiler's
 * pointer width so this matches the host's ssize_t on the Linux box
 * used for libc unit tests (and stays 32-bit on bboeos). */
typedef ptrdiff_t ssize_t;
#endif
