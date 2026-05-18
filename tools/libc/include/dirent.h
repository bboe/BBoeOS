#ifndef BBOEOS_LIBC_DIRENT_H
#define BBOEOS_LIBC_DIRENT_H

#include <sys/types.h>

/* dirent.h — minimal POSIX directory iteration on top of
 * SYS_IO_GETDENTS.  d_name is a fixed 256-byte buffer (BBoeOS kernel
 * caps names at <= 255 chars + NUL).  The d_ino / d_type fields are
 * copied straight from the kernel's variable-length getdents records;
 * other POSIX-optional members (d_off, d_reclen) aren't exposed
 * because the kernel buffer is owned by the libc DIR, not the
 * caller. */

struct dirent {
    ino_t d_ino;
    unsigned char d_type;
    char d_name[256];
};

#define DT_DIR 4
#define DT_REG 8
#define DT_UNKNOWN 0

typedef struct DIR DIR;

int closedir(DIR *directory);
DIR *opendir(const char *path);
struct dirent *readdir(DIR *directory);
void rewinddir(DIR *directory);

#endif
