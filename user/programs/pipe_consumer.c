/* pipe_consumer — reads from stdin in different patterns.

   Argv subcommands:
     (no arg) — count bytes, print decimal + newline
     slow     — read 1 byte at a time, echo to stdout
     fail     — read one byte then exit 9 (status check)
*/

int strcmp(const char *a, const char *b) {
    int index = 0;
    while (1) {
        if (a[index] != b[index]) {
            return a[index] - b[index];
        }
        if (a[index] == '\0') {
            return 0;
        }
        index += 1;
    }
}

int main(int argc, char *argv[]) {
    if (argc >= 2 && strcmp(argv[1], "slow") == 0) {
        char b;
        int n;
        while (1) {
            n = read(STDIN, &b, 1);
            if (n <= 0) {
                break;
            }
            write(STDOUT, &b, 1);
        }
        return 0;
    }
    if (argc >= 2 && strcmp(argv[1], "fail") == 0) {
        char b;
        read(STDIN, &b, 1);
        return 9;
    }
    /* Default: count bytes, print decimal + newline. */
    char buffer[256];
    int total = 0;
    int n;
    while (1) {
        n = read(STDIN, buffer, 256);
        if (n <= 0) {
            break;
        }
        total += n;
    }
    /* Print decimal count + newline. */
    char digits[16];
    int d = 0;
    if (total == 0) {
        digits[d] = '0';
        d += 1;
    } else {
        char rev[16];
        int r = 0;
        while (total > 0) {
            rev[r] = '0' + (total - (total / 10) * 10);
            total = total / 10;
            r += 1;
        }
        while (r > 0) {
            r -= 1;
            digits[d] = rev[r];
            d += 1;
        }
    }
    digits[d] = '\n';
    write(STDOUT, digits, d + 1);
    return 0;
}
