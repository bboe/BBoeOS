#include "getopt.h"
#include "strtol.h"

int strcmp(const char *left, const char *right);

/* Heap layout (68 KB = 0x11000 bytes):
   [0x00000, 0x0F000)  data_buffer        (60 KB, line bytes null-terminated)
   [0x0F000, 0x10000)  line_pointers      (1024 entries x 4 bytes = 4 KB)
   [0x10000, 0x11000)  scratch_pointers   (1024 entries x 4 bytes = 4 KB)
*/
#define DATA_CAPACITY 0xF000
#define HEAP_SIZE 0x11000
#define MAX_LINES 1024

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of the helpers it calls.  cc.py's
   whole-file pre-pass resolves these without prototypes. */
int compare_lines(char *left, char *right, int numeric, int reverse);
void merge_pass(char **source, char **destination, int line_count,
                int run_width, int numeric, int reverse);
int numeric_compare(char *left, char *right);

int compare_lines(char *left, char *right, int numeric, int reverse) {
    int result;
    if (numeric) {
        result = numeric_compare(left, right);
    } else {
        result = strcmp(left, right);
    }
    if (reverse) {
        return -result;
    }
    return result;
}

int main(int argc, char *argv[]) {
    int reverse = 0;
    int numeric = 0;
    int unique = 0;
    int option = getopt(argc, argv, "rnu");
    while (option != -1) {
        if (option == 'r') {
            reverse = 1;
        } else if (option == 'n') {
            numeric = 1;
        } else if (option == 'u') {
            unique = 1;
        } else {
            die("sort: bad flag\n");
        }
        option = getopt(argc, argv, "rnu");
    }
    if (argc - optind > 1) {
        die("sort: too many arguments\n");
    }
    char *path = optind < argc ? argv[optind] : NULL;
    int fd = STDIN;
    if (path != NULL) {
        fd = open(path, O_RDONLY);
        if (fd < 0) {
            die("sort: open failed\n");
        }
    }
    /* Acquire a 68 KB heap.  Layout:
       [0, DATA_CAPACITY)            line bytes (null-terminated in place)
       [DATA_CAPACITY, ...)          line_pointers[MAX_LINES]   (4 KB)
       [DATA_CAPACITY + 4 KB, ...)   scratch_pointers[MAX_LINES] (4 KB)
       Total: 0x11000 bytes.
    */
    char *heap_base = sys_break(0);
    char *heap_end = heap_base + HEAP_SIZE;
    char *grown = sys_break(heap_end);
    if (grown != heap_end) {
        die("sort: heap allocation failed\n");
    }
    char *data_buffer = heap_base;
    char **line_pointers = (char **)(heap_base + DATA_CAPACITY);
    char **scratch_pointers =
        (char **)(heap_base + DATA_CAPACITY + MAX_LINES * sizeof(char *));
    int position = 0;
    int line_count = 0;
    int in_line = 0;
    while (1) {
        if (position >= DATA_CAPACITY) {
            die("sort: input too large\n");
        }
        int bytes_read =
            read(fd, data_buffer + position, DATA_CAPACITY - position);
        if (bytes_read < 0) {
            die("sort: read failed\n");
        }
        if (bytes_read == 0) {
            break;
        }
        int chunk_end = position + bytes_read;
        while (position < chunk_end) {
            if (in_line == 0) {
                if (line_count >= MAX_LINES) {
                    die("sort: too many lines\n");
                }
                line_pointers[line_count] = data_buffer + position;
                line_count += 1;
                in_line = 1;
            }
            if (data_buffer[position] == '\n') {
                data_buffer[position] = '\0';
                in_line = 0;
            }
            position += 1;
        }
    }
    if (in_line) {
        if (position >= DATA_CAPACITY) {
            die("sort: input too large\n");
        }
        data_buffer[position] = '\0';
        position += 1;
    }
    if (path != NULL) {
        close(fd);
    }
    char **source = line_pointers;
    char **destination = scratch_pointers;
    int run_width = 1;
    while (run_width < line_count) {
        merge_pass(source, destination, line_count, run_width, numeric,
                   reverse);
        char **swap = source;
        source = destination;
        destination = swap;
        run_width = run_width + run_width;
    }
    int output_index = 0;
    while (output_index < line_count) {
        if (unique && output_index > 0) {
            if (compare_lines(source[output_index - 1], source[output_index],
                              numeric, reverse) == 0) {
                output_index += 1;
                continue;
            }
        }
        char *line = source[output_index];
        int length = strlen(line);
        write(STDOUT, line, length);
        write(STDOUT, "\n", 1);
        output_index += 1;
    }
    return 0;
}

void merge_pass(char **source, char **destination, int line_count,
                int run_width, int numeric, int reverse) {
    int start = 0;
    while (start < line_count) {
        int middle = start + run_width;
        if (middle > line_count) {
            middle = line_count;
        }
        int end = start + run_width + run_width;
        if (end > line_count) {
            end = line_count;
        }
        int left_index = start;
        int right_index = middle;
        int output_index = start;
        while (left_index < middle && right_index < end) {
            if (compare_lines(source[left_index], source[right_index], numeric,
                              reverse) <= 0) {
                destination[output_index] = source[left_index];
                left_index += 1;
            } else {
                destination[output_index] = source[right_index];
                right_index += 1;
            }
            output_index += 1;
        }
        while (left_index < middle) {
            destination[output_index] = source[left_index];
            left_index += 1;
            output_index += 1;
        }
        while (right_index < end) {
            destination[output_index] = source[right_index];
            right_index += 1;
            output_index += 1;
        }
        start = end;
    }
}

int numeric_compare(char *left, char *right) {
    int left_value = strtol(left, NULL, 10);
    int right_value = strtol(right, NULL, 10);
    if (left_value != right_value) {
        return (left_value < right_value) ? -1 : 1;
    }
    return strcmp(left, right);
}
