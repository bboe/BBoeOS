#include "getopt.h"
#include "line_helpers.h"

void emit(char *line, int line_length, int run_count, int show_counts) {
    if (show_counts) {
        printf("%d ", run_count);
    }
    write(STDOUT, line, line_length);
    putchar('\n');
}

int main(int argc, char *argv[]) {
    int show_counts = 0;
    int dups_only = 0;
    int option = getopt(argc, argv, "cd");
    while (option != -1) {
        if (option == 'c') {
            show_counts = 1;
        } else if (option == 'd') {
            dups_only = 1;
        } else {
            die("uniq: bad flag\n");
        }
        option = getopt(argc, argv, "cd");
    }
    if (argc - optind > 1) {
        die("uniq: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    int fd = STDIN;
    if (path != NULL) {
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            die("uniq: open failed\n");
        }
    }
    char previous[MAX_LINE];
    char current[MAX_LINE];
    int previous_length = 0;
    int run_count = 0;
    int has_previous = 0;
    while (1) {
        int line_length = read_line(fd, current, MAX_LINE);
        if (line_length <= 0) {
            break;
        }
        int strip_newline = current[line_length - 1] == '\n' ? 1 : 0;
        int current_length = line_length - strip_newline;
        if (has_previous && previous_length == current_length && memcmp(previous, current, current_length) == 0) {
            run_count += 1;
        } else {
            if (has_previous && (!dups_only || run_count > 1)) {
                emit(previous, previous_length, run_count, show_counts);
            }
            memcpy(previous, current, current_length);
            previous_length = current_length;
            run_count = 1;
            has_previous = 1;
        }
    }
    if (has_previous && (!dups_only || run_count > 1)) {
        emit(previous, previous_length, run_count, show_counts);
    }
    if (path != NULL) {
        close(fd);
    }
    return 0;
}
