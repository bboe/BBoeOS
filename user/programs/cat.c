int main(int argc, char *argv[]) {
    char buffer[512];
    int fd;
    int needs_close = 0;
    if (argc == 1) {
        fd = STDIN;
    } else if (argc == 2) {
        fd = open(argv[1], O_RDONLY);
        if (fd < 0) {
            die("File not found\n");
        }
        needs_close = 1;
    } else {
        die("Usage: cat [filename]\n");
    }
    while (1) {
        int bytes = read(fd, buffer, 512);
        if (bytes <= 0) {
            break;
        }
        write(STDOUT, buffer, bytes);
    }
    if (needs_close) {
        close(fd);
    }
    return 0;
}
