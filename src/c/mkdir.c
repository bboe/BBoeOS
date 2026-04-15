void main(char *arg) {
    if (arg == NULL) {
        printf("Usage: mkdir <name>\n");
        return;
    }
    int error = mkdir(arg);
    if (!error) {
        return;
    }
    if (error == 1) {
        printf("Directory full\n");
    } else if (error == 2) {
        printf("Already exists\n");
    } else {
        printf("Error\n");
    }
}
