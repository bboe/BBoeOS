/* user/libbboeos/dirent.c — POSIX directory iteration on SYS_IO_GETDENTS.
 *
 * Each DIR owns a 4 KB receive buffer.  readdir() refills it via
 * getdents(2) when the cursor catches up with the high-water mark and
 * parses one variable-length record per call into a single struct
 * dirent owned by the DIR (POSIX: subsequent readdir() invalidates
 * the previous return).
 *
 * Record layout (matches the kernel's dir_emit packing):
 *   offset 0  uint32 d_ino
 *   offset 4  uint16 d_reclen   (round-up of 7 + namelen + 1 to 4)
 *   offset 6  uint8  d_type
 *   offset 7  char   d_name[]   (NUL-terminated, padded to align)
 */

#include <dirent.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Kernel-side syscall wrapper (sibling of read / write in syscall.c). */
int getdents(int fd, void *buffer, int count);

struct DIR {
    int fd;
    int buffer_bytes;
    int buffer_cursor;
    struct dirent entry;
    unsigned char buffer[4096];
};

int closedir(DIR *directory) {
    if (directory == NULL) {
        errno = EINVAL;
        return -1;
    }
    int result = close(directory->fd);
    free(directory);
    return result;
}

DIR *opendir(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return NULL;
    DIR *directory = malloc(sizeof *directory);
    if (directory == NULL) {
        close(fd);
        errno = ENOMEM;
        return NULL;
    }
    directory->fd = fd;
    directory->buffer_bytes = 0;
    directory->buffer_cursor = 0;
    return directory;
}

struct dirent *readdir(DIR *directory) {
    if (directory->buffer_cursor >= directory->buffer_bytes) {
        int bytes = getdents(directory->fd, directory->buffer,
                             (int)sizeof directory->buffer);
        if (bytes <= 0)
            return NULL;
        directory->buffer_bytes = bytes;
        directory->buffer_cursor = 0;
    }
    unsigned char *record = directory->buffer + directory->buffer_cursor;
    unsigned int d_ino;
    unsigned short d_reclen;
    memcpy(&d_ino, record + 0, sizeof d_ino);
    memcpy(&d_reclen, record + 4, sizeof d_reclen);
    directory->entry.d_ino = (ino_t)d_ino;
    directory->entry.d_type = record[6];
    /* Names from the wire are always NUL-terminated and <= 255 chars,
     * so strcpy into d_name[256] is safe.  If the kernel ever lifts
     * the cap, cap here too. */
    strcpy(directory->entry.d_name, (const char *)(record + 7));
    directory->buffer_cursor += d_reclen;
    return &directory->entry;
}

void rewinddir(DIR *directory) {
    if (directory == NULL)
        return;
    lseek(directory->fd, 0, SEEK_SET);
    directory->buffer_bytes = 0;
    directory->buffer_cursor = 0;
}
