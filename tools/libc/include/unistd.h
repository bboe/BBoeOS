#ifndef BBOEOS_LIBC_UNISTD_H
#define BBOEOS_LIBC_UNISTD_H
#include <stddef.h>
#include <sys/types.h>

#define O_CREAT  0x40
#define O_RDONLY 0
#define O_RDWR   2
#define O_WRONLY 1
#define SEEK_CUR 1
#define SEEK_END 2
#define SEEK_SET 0

void    _exit(int status) __attribute__((noreturn));
int     brk(void *addr);
int     close(int fd);
int     ioctl(int fd, int cmd, unsigned int ecx_arg, unsigned int edx_arg);
off_t   lseek(int fd, off_t offset, int whence);    /* stub: returns -1, sets errno=ESPIPE */
int     open(const char *path, int flags, ...);
ssize_t read(int fd, void *buf, size_t count);
void   *sbrk(ptrdiff_t increment);
ssize_t write(int fd, const void *buf, size_t count);

#endif
