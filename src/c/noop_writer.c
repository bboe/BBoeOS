int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: noop_writer <path>\n");
    }
    int fd = open(argv[0], O_WRONLY);
    if (fd < 0) {
        die("open failed\n");
    }
    close(fd);
    return 0;
}
