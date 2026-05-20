int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: rm <file>\n");
    }
    int error = unlink(argv[1]);
    if (!error) {
        return 0;
    }
    if (error == ERROR_PROTECTED) {
        die("File is protected\n");
    } else {
        die("File not found\n");
    }
}
