#ifndef BBOEOS_LIBC_FCNTL_H
#define BBOEOS_LIBC_FCNTL_H
/* O_* flags + open() declaration.  Our open() (in syscall.c) lives
 * in unistd.h; we re-include it here so callers that #include <fcntl.h>
 * for open() without also reaching for <unistd.h> still see the
 * declaration.  Same idea as glibc's fcntl.h. */
#include <unistd.h>
#endif
