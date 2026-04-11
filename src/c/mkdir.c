void main(char *arg) {
    if (arg == 0) {
        puts("Usage: mkdir <name>\n");
        return;
    }
    int error = mkdir(arg);
    if (!error) {
        return;
    }
    if (error == 1) {
        puts("Directory full\n");
    } else if (error == 2) {
        puts("Already exists\n");
    } else {
        puts("Error\n");
    }
}
