/* ls — list the entries of a directory, sorted alphabetically.

   Uses SYS_IO_GETDENTS so the iteration is fd-type agnostic across
   bbfs and ext2; the kernel emits one variable-length record per
   live entry into the user buffer.  Records are packed:
       offset 0  uint32 d_ino
       offset 4  uint16 d_reclen
       offset 6  uint8  d_type
       offset 7  char   d_name[]   (null-terminated, padded to align)

   Suffixes follow POSIX `ls` (no flags): `/` after a subdirectory,
   nothing after a regular file.  POSIX `ls` only adds the `*`
   execute marker with `-F`, and stat() is currently a libc stub,
   so dropping `*` matches the POSIX default surface area.

   Names are copied out of the rotating getdents buffer into a
   scratch arena so they survive subsequent getdents calls; then
   the arena pointers + per-entry is_dir flags are insertion-sorted
   by strcmp.  48 root entries × 25 bytes max gives ~1200 bytes of
   arena worst case; the 2048-byte allocation has headroom. */

#define ARENA_BYTES 2048
#define BUFFER_BYTES 4096
#define DT_DIR 4
#define MAX_ENTRIES 64

int strcmp(const char *a, const char *b);

int main(int argc, char *argv[]) {
    char *path = ".";
    if (argc > 1) {
        path = argv[1];
    }
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        die("Not found\n");
    }

    char buffer[BUFFER_BYTES];
    char arena[ARENA_BYTES];
    char *names[MAX_ENTRIES];
    char is_dir[MAX_ENTRIES];
    int arena_used = 0;
    int count = 0;

    while (1) {
        int bytes = getdents(fd, buffer, BUFFER_BYTES);
        if (bytes <= 0) {
            break;
        }
        int cursor = 0;
        while (cursor < bytes) {
            int reclen = buffer[cursor + 4] + (buffer[cursor + 5] << 8);
            int type = buffer[cursor + 6];
            char *src = buffer + cursor + 7;
            int len = strlen(src) + 1;
            memcpy(arena + arena_used, src, len);
            names[count] = arena + arena_used;
            is_dir[count] = (type == DT_DIR);
            arena_used = arena_used + len;
            count = count + 1;
            cursor = cursor + reclen;
        }
    }
    close(fd);

    /* Insertion sort by strcmp(name_a, name_b).  Max ~48 entries in
       a root dir today; the O(N^2) cost is invisible for N this
       small and keeps the code independent of cc.py's qsort (we
       don't have one in shipped programs). */
    int i = 1;
    while (i < count) {
        char *key_name = names[i];
        char key_dir = is_dir[i];
        int j = i - 1;
        while (j >= 0) {
            if (strcmp(names[j], key_name) <= 0) {
                break;
            }
            names[j + 1] = names[j];
            is_dir[j + 1] = is_dir[j];
            j = j - 1;
        }
        names[j + 1] = key_name;
        is_dir[j + 1] = key_dir;
        i = i + 1;
    }

    i = 0;
    while (i < count) {
        write(STDOUT, names[i], strlen(names[i]));
        if (is_dir[i] != '\0') {
            putchar('/');
        }
        putchar('\n');
        i = i + 1;
    }
    return 0;
}
