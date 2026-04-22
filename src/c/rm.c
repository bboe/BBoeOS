int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: rm <file>\n");
    }
    int error = unlink(argv[0]);
    if (!error) {
        return 0;
    }
    if (error == ERROR_PROTECTED) {
        die("File is protected\n");
    } else {
        die("File not found\n");
    }
}
