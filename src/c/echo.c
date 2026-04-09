void main(int argc, char *argv[]) {
    int i = 0;
    while (i < argc) {
	if (i > 0) {
	    putc(' ');
	}
        puts(argv[i]);
        i = i + 1;
    }
    putc('\n');
}
