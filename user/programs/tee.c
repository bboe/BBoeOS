int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: tee <file>\n");
    }
    int out = open(argv[1], O_WRONLY + O_CREAT + O_TRUNC, 0);
    if (out < 0) {
        die("tee: open failed\n");
    }
    char buffer[512];
    while (1) {
        int bytes_read = read(STDIN, buffer, 512);
        if (bytes_read <= 0) {
            break;
        }
        write(STDOUT, buffer, bytes_read);
        write(out, buffer, bytes_read);
    }
    close(out);
    return 0;
}
