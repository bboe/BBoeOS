int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: cp <srcname> <destname>\n");
    }
    int source_fd = open(argv[0], O_RDONLY);
    if (source_fd < 0) {
        die("File not found\n");
    }
    int mode = fstat(source_fd);
    int destination_fd = open(argv[1], O_WRONLY + O_CREAT, mode);
    if (destination_fd < 0) {
        close(source_fd);
        die("File already exists\n");
    }
    char *buffer = SECTOR_BUFFER;
    int bytes;
    do {
        bytes = read(source_fd, buffer, 512);
        write(destination_fd, buffer, bytes);
    } while (bytes > 0);
    close(destination_fd);
    close(source_fd);
}
