void main(char *arg) {
    if (arg == 0) {
        die("Usage: cat <filename>\n");
    }
    int fd = open(arg, O_RDONLY);
    if (fd < 0) {
        die("File not found\n");
    }
    char *buffer = DISK_BUFFER;
    int bytes;
    do {
        bytes = read(fd, buffer, 512);
        print_buffer(buffer, bytes);
    } while (bytes > 0);
    close(fd);
}
