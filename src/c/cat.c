void main(char *arg) {
    if (arg == 0) {
        die("Usage: cat <filename>\n");
    }
    char *buffer = DISK_BUFFER;
    int sector = fs_find(arg, "Is a directory\n");
    if (!sector) {
        die("File not found\n");
    }
    while (1) {
        int bytes = fs_read(sector, buffer);
        if (!bytes) {
            break;
        }
        print_buffer(buffer, bytes);
        sector += 1;
    }
}
