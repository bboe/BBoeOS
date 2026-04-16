int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: cat <filename>\n");
    }
    int fd = open(argv[0], O_RDONLY);
    if (fd < 0) {
        die("File not found\n");
    }
    char *buffer = SECTOR_BUFFER;
    int bytes;
    do {
        bytes = read(fd, buffer, 512);
        write(STDOUT, buffer, bytes);
    } while (bytes > 0);
    close(fd);
}
