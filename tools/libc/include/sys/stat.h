#ifndef BBOEOS_LIBC_SYS_STAT_H
#define BBOEOS_LIBC_SYS_STAT_H
/* Minimal stat() shim — Doom calls stat() to probe for IWAD candidates.
 * We surface only the fields Doom reads (st_mode for the S_IS* macros)
 * and a stub stat() that returns -1 / ENOENT so the IWAD search falls
 * through to whatever path we hand it on the command line. */
#include <sys/types.h>

#define S_IFDIR    0040000
#define S_IFMT     0170000
#define S_IFREG    0100000
#define S_ISDIR(m) (((m) & S_IFMT) == S_IFDIR)
#define S_ISREG(m) (((m) & S_IFMT) == S_IFREG)

struct stat {
    unsigned int st_mode;
    off_t        st_size;
};

int mkdir(const char *path, int mode);    /* stub: returns -1, sets errno=ENOSYS */
int stat(const char *path, struct stat *buf);

#endif
