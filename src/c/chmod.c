int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: chmod [+x|-x] <file>\n");
    }
    char *mode = argv[0];
    int flags;
    if (mode[0] == '+') {
        flags = FLAG_EXECUTE;
    } else if (mode[0] == '-') {
        flags = 0;
    } else {
        die("Usage: chmod [+x|-x] <file>\n");
    }
    if (mode[1] != 'x') {
        die("Usage: chmod [+x|-x] <file>\n");
    }
    int error = chmod(argv[1], flags);
    if (!error) {
        return 0;
    }
    if (error == ERROR_PROTECTED) {
        die("File is protected\n");
    } else {
        die("File not found\n");
    }
}
