int main(int argc, char *argv[]) {
    if (argc != 3) {
        die("Usage: mv <oldname> <newname>\n");
    }
    if (strlen(argv[2]) > 24) {
        die("Name too long (max 26 chars)\n");
    }
    int error = rename(argv[1], argv[2]);
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
