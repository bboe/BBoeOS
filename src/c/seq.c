#include "strtol.h"

int main(int argc, char *argv[]) {
    if (argc < 2 || argc > 3) {
        die("Usage: seq [start] end\n");
    }
    int start = 1;
    int end;
    if (argc == 2) {
        end = strtol(argv[1], NULL, 10);
    } else {
        start = strtol(argv[1], NULL, 10);
        end = strtol(argv[2], NULL, 10);
    }
    int value = start;
    while (value <= end) {
        printf("%d\n", value);
        value += 1;
    }
    return 0;
}
