int main(int argc, char *argv[]) {
    int n = 0;
    if (argc >= 2) {
        char *p = argv[1];
        int index = 0;
        while (p[index] >= '0' && p[index] <= '9') {
            n = n * 10 + (p[index] - '0');
            index = index + 1;
        }
    }
    _exit(n);
}
