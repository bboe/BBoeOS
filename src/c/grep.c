#include "ctype.h"
#include "getopt.h"
#include "line_helpers.h"

void fold_to_lower(char *destination, char *source, int length) {
    int i = 0;
    while (i < length) {
        destination[i] = tolower(source[i]);
        i += 1;
    }
}

int line_matches(char *pattern, int pattern_length, char *line, int line_length) {
    if (pattern_length > line_length) {
        return 0;
    }
    int start = 0;
    while (start <= line_length - pattern_length) {
        if (memcmp(line + start, pattern, pattern_length) == 0) {
            return 1;
        }
        start += 1;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    int invert = 0;
    int show_line_numbers = 0;
    int ignore_case = 0;
    int option = getopt(argc, argv, "vni");
    while (option != -1) {
        if (option == 'v') {
            invert = 1;
        } else if (option == 'n') {
            show_line_numbers = 1;
        } else if (option == 'i') {
            ignore_case = 1;
        } else {
            die("grep: bad flag\n");
        }
        option = getopt(argc, argv, "vni");
    }
    if (optind >= argc) {
        die("Usage: grep [-vni] <pattern> [file]\n");
    }
    char *pattern = argv[optind];
    optind += 1;
    if (argc - optind > 1) {
        die("grep: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    int fd = STDIN;
    if (path != NULL) {
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            die("grep: open failed\n");
        }
    }
    int pattern_length = strlen(pattern);
    if (ignore_case) {
        fold_to_lower(pattern, pattern, pattern_length);
    }
    int line_number = 0;
    int any_match = 0;
    char buffer[MAX_LINE];
    char folded[MAX_LINE];
    while (1) {
        int line_length = read_line(fd, buffer, MAX_LINE);
        if (line_length <= 0) {
            break;
        }
        line_number += 1;
        int strip_newline = (line_length > 0 && buffer[line_length - 1] == '\n') ? 1 : 0;
        int match_length = line_length - strip_newline;
        char *haystack = buffer;
        if (ignore_case) {
            fold_to_lower(folded, buffer, match_length);
            haystack = folded;
        }
        int matched = line_matches(pattern, pattern_length, haystack, match_length);
        if (matched == invert) {
            continue;
        }
        any_match = 1;
        if (show_line_numbers) {
            printf("%d:", line_number);
        }
        write(STDOUT, buffer, line_length);
    }
    if (path != NULL) {
        close(fd);
    }
    return any_match ? 0 : 1;
}
