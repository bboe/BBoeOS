int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: mv <oldname> <newname>\n");
    }
    if (strlen(argv[1]) > 24) {
        die("Name too long (max 26 chars)\n");
    }
    int error = rename(argv[0], argv[1]);
    if (!error) {
        return 0;
    }
    if (error == ERROR_EXISTS) {
        die("File already exists\n");
    }
    if (error == ERROR_PROTECTED) {
        die("File is protected\n");
    }
    die("File not found\n");
}
