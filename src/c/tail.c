#include "getopt.h"
#include "strtol.h"

#define TAIL_BUFFER_BYTES 65536

char tail_buffer[TAIL_BUFFER_BYTES];

int main(int argc, char *argv[]) {
    int want = 10;
    int option = getopt(argc, argv, "n:");
    while (option != -1) {
        if (option == 'n') {
            want = strtol(optarg, NULL, 10);
        } else {
            die("tail: bad flag\n");
        }
        option = getopt(argc, argv, "n:");
    }
    if (argc - optind > 1) {
        die("tail: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    if (path == NULL) {
        die("tail: stdin support lands in PR 2 — pass a file\n");
    }
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        die("tail: open failed\n");
    }
    int total = 0;
    while (total < TAIL_BUFFER_BYTES) {
        int bytes_read = read(fd, tail_buffer + total, TAIL_BUFFER_BYTES - total);
        if (bytes_read <= 0) {
            break;
        }
        total += bytes_read;
    }
    /* If there's still more file left, refuse rather than producing wrong
       output. PR 2 lifts this with stdin / streaming support. */
    char overflow;
    int extra = read(fd, &overflow, 1);
    close(fd);
    if (extra > 0) {
        die("tail: file too large (>64 KB)\n");
    }
    /* Walk back from end, counting newlines. Treat a trailing '\n' as the
       terminator of the last line, not as a separate empty line. */
    int index = total;
    int found = 0;
    if (index > 0 && tail_buffer[index - 1] == '\n') {
        index -= 1;
    }
    while (index > 0 && found < want) {
        index -= 1;
        if (tail_buffer[index] == '\n') {
            found += 1;
            if (found == want) {
                index += 1;
                break;
            }
        }
    }
    int remaining = total - index;
    if (remaining > 0) {
        write(STDOUT, tail_buffer + index, remaining);
    }
    return 0;
}
