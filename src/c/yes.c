int main(int argc, char *argv[]) {
    if (argc > 2) {
        die("yes: too many arguments\n");
    }
    char *word = argc == 2 ? argv[1] : "y";
    int length = strlen(word);
    while (1) {
        write(STDOUT, word, length);
        write(STDOUT, "\n", 1);
    }
    return 0;
}
