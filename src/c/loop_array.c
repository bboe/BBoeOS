int main() {
    char *messages[] = {"a", "b", "c"};
    int i = 0;
    while (i < sizeof(messages) / sizeof(char*)) {
        int len = strlen(messages[i]);
        write(STDOUT, messages[i], len);
        i += 1;
    }
    putchar('\n');
}
