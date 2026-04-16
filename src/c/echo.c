int main(int argc, char *argv[]) {
    int i = 0;
    while (i < argc) {
	if (i > 0) {
	    putchar(' ');
	}
        int len = strlen(argv[i]);
        write(STDOUT, argv[i], len);
        i = i + 1;
    }
    putchar('\n');
}
