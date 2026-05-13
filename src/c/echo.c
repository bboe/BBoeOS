int main(int argc, char *argv[]) {
    int i = 1;
    while (i < argc) {
	if (i > 1) {
	    putchar(' ');
	}
        int len = strlen(argv[i]);
        write(STDOUT, argv[i], len);
        i += 1;
    }
    putchar('\n');
}
