#include "getopt.h"
#include "line_helpers.h"

char is_space[256];

int main(int argc, char *argv[]) {
    is_space[' '] = 1;
    is_space['\t'] = 1;
    is_space['\n'] = 1;
    is_space['\r'] = 1;
    int show_lines = 0;
    int show_words = 0;
    int show_bytes = 0;
    int option = getopt(argc, argv, "lwc");
    while (option != -1) {
        if (option == 'l') {
            show_lines = 1;
        } else if (option == 'w') {
            show_words = 1;
        } else if (option == 'c') {
            show_bytes = 1;
        } else {
            die("wc: bad flag\n");
        }
        option = getopt(argc, argv, "lwc");
    }
    if (argc - optind > 1) {
        die("wc: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    if (!show_lines && !show_words && !show_bytes) {
        show_lines = 1;
        show_words = 1;
        show_bytes = 1;
    }
    int fd = STDIN;
    if (path != NULL) {
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            die("wc: open failed\n");
        }
    }
    int lines = 0;
    int words = 0;
    int bytes = 0;
    char buffer[MAX_LINE];
    while (1) {
        int line_length = read_line(fd, buffer, MAX_LINE);
        if (line_length <= 0) {
            break;
        }
        bytes += line_length;
        if (buffer[line_length - 1] == '\n') {
            lines += 1;
        }
        int in_word = 0;
        int i = 0;
        while (i < line_length) {
            if (is_space[buffer[i] & 0xFF] != '\0') {
                in_word = 0;
            } else if (!in_word) {
                in_word = 1;
                words += 1;
            }
            i += 1;
        }
    }
    if (path != NULL) {
        close(fd);
    }
    int first = 1;
    if (show_lines) {
        printf("%d", lines);
        first = 0;
    }
    if (show_words) {
        if (!first) {
            putchar(' ');
        }
        printf("%d", words);
        first = 0;
    }
    if (show_bytes) {
        if (!first) {
            putchar(' ');
        }
        printf("%d", bytes);
    }
    putchar('\n');
    return 0;
}
