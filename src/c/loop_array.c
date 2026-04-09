void main() {
    char *messages[] = {"a", "b", "c"};
    int i = 0;
    while (i < 3) {
        puts(messages[i]);
        i += 1;
    }
    putc('\n');
}
