void main() {
    char *messages[] = {"a", "b", "c"};
    int i = 0;
    while (i < sizeof(messages) / sizeof(char*)) {
        puts(messages[i]);
        i = i + 1;
    }
    putc('\n');
}
