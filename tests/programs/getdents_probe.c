/* getdents_probe — exercise SYS_IO_GETDENTS on a directory and print
   one `type=N name=X` line per record.  Used by test_bboefs.py to
   verify the kernel's getdents implementation across bbfs and ext2
   without requiring a libc.  Reports `EOF` after the last record. */

int main(int argc, char *argv[]) {
    char *path = argc > 1 ? argv[1] : ".";
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        die("getdents_probe: open failed\n");
    }
    char buffer[4096];
    while (1) {
        int bytes = getdents(fd, buffer, 4096);
        if (bytes <= 0) {
            break;
        }
        int cursor = 0;
        while (cursor < bytes) {
            int reclen = buffer[cursor + 4] + (buffer[cursor + 5] << 8);
            int type   = buffer[cursor + 6];
            char *name = buffer + cursor + 7;
            printf("type=%d name=%s\n", type, name);
            cursor = cursor + reclen;
        }
    }
    printf("EOF\n");
    close(fd);
    return 0;
}
