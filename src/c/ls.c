void main(char *arg) {
    char *name = ".";
    if (arg != 0) {
        if (arg[0] != 0) {
            name = arg;
        }
    }
    int fd = open(name, O_RDONLY);
    if (fd < 0) {
        die("Not found\n");
    }
    char *entry = SECTOR_BUFFER;
    while (1) {
        int bytes = read(fd, entry, 32);
        if (bytes == 0) {
            break;
        }
        int len = strlen(entry);
        write(STDOUT, entry, len);
        int flags = entry[25];
        if (flags == FLAG_DIRECTORY) {
            putc('/');
        } else if (flags == FLAG_EXECUTE) {
            putc('*');
        }
        putc('\n');
    }
    close(fd);
}
