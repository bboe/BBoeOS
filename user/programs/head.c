#include "getopt.h"
#include "line_helpers.h"
#include "strtol.h"

int main(int argc, char *argv[]) {
    int limit = 10;
    int option = getopt(argc, argv, "n:");
    while (option != -1) {
        if (option == 'n') {
            limit = strtol(optarg, NULL, 10);
        } else {
            die("head: bad flag\n");
        }
        option = getopt(argc, argv, "n:");
    }
    if (argc - optind > 1) {
        die("head: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    int fd = STDIN;
    if (path != NULL) {
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            die("head: open failed\n");
        }
    }
    int printed = 0;
    char buffer[MAX_LINE];
    while (printed < limit) {
        int line_length = read_line(fd, buffer, MAX_LINE);
        if (line_length <= 0) {
            break;
        }
        write(STDOUT, buffer, line_length);
        printed += 1;
    }
    if (path != NULL) {
        close(fd);
    }
    return 0;
}
