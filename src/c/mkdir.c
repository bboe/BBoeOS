int main(int argc, char *argv[]) {
    if (argc != 2) {
        die("Usage: mkdir <name>\n");
    }
    int error = mkdir(argv[1]);
    if (!error) {
        return 0;
    }
    if (error == 1) {
        printf("Directory full\n");
    } else if (error == 2) {
        printf("Already exists\n");
    } else {
        printf("Error\n");
    }
}
