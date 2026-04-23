char entry[DIRECTORY_ENTRY_SIZE];

int main(int argc, char *argv[]) {
    char *name = ".";
    if (argc > 0) {
        name = argv[0];
    }
    int fd = open(name, O_RDONLY);
    if (fd < 0) {
        die("Not found\n");
    }
    while (1) {
        int bytes = read(fd, entry, DIRECTORY_ENTRY_SIZE);
        if (bytes == 0) {
            break;
        }
        int len = strlen(entry);
        write(STDOUT, entry, len);
        int flags = entry[DIRECTORY_OFFSET_FLAGS];
        if (flags == FLAG_DIRECTORY) {
            putchar('/');
        } else if (flags == FLAG_EXECUTE) {
            putchar('*');
        }
        putchar('\n');
    }
    close(fd);
}
