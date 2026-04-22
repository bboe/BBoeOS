int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: rmdir <dir>\n");
    }
    int error = rmdir(argv[0]);
    if (!error) {
        return 0;
    }
    if (error == ERROR_NOT_FOUND) {
        die("Not found\n");
    } else if (error == ERROR_NOT_EMPTY) {
        die("Not empty\n");
    } else {
        die("Error\n");
    }
}
