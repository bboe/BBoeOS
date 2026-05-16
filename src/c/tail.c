#include "getopt.h"
#include "line_helpers.h"
#include "strtol.h"

#define TAIL_BUFFER_BYTES 65536
#define MAX_TAIL_LINES 4096

int newline_positions[MAX_TAIL_LINES];
char tail_buffer[TAIL_BUFFER_BYTES];

void run_file_mode(char *path, int want) {
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
    char overflow;
    int extra = read(fd, &overflow, 1);
    close(fd);
    if (extra > 0) {
        die("tail: file too large (>64 KB)\n");
    }
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
}

void run_stdin_mode(int want) {
    if (want >= MAX_TAIL_LINES) {
        die("tail: N too large\n");
    }
    if (want == 0) {
        return;
    }
    int head = 0;
    int tail = 0;
    int has_bytes = 0;
    int last_was_newline = 0;
    int total_newlines = 0;
    int queue_head = 0;
    int queue_count = 0;
    int queue_capacity = want + 1;
    char input_chunk[1024];
    while (1) {
        int bytes_read = read(STDIN, input_chunk, 1024);
        if (bytes_read <= 0) {
            break;
        }
        int k = 0;
        while (k < bytes_read) {
            char byte = input_chunk[k];
            int next_tail = (tail + 1) % TAIL_BUFFER_BYTES;
            if (has_bytes && next_tail == head) {
                if (queue_count > 0) {
                    head = (newline_positions[queue_head] + 1) % TAIL_BUFFER_BYTES;
                    queue_head = (queue_head + 1) % queue_capacity;
                    queue_count -= 1;
                } else {
                    head = (head + 1) % TAIL_BUFFER_BYTES;
                }
            }
            tail_buffer[tail] = byte;
            if (byte == '\n') {
                if (queue_count == queue_capacity) {
                    head = (newline_positions[queue_head] + 1) % TAIL_BUFFER_BYTES;
                    queue_head = (queue_head + 1) % queue_capacity;
                    queue_count -= 1;
                }
                int push_at = (queue_head + queue_count) % queue_capacity;
                newline_positions[push_at] = tail;
                queue_count += 1;
                total_newlines += 1;
                last_was_newline = 1;
            } else {
                last_was_newline = 0;
            }
            tail = next_tail;
            has_bytes = 1;
            k += 1;
        }
    }
    if (!has_bytes) {
        return;
    }
    int total_lines = total_newlines + (last_was_newline ? 0 : 1);
    int start;
    if (total_lines <= want) {
        start = head;
    } else {
        int skip = total_lines - want;
        int oldest_in_queue = total_newlines - queue_count + 1;
        int offset = skip - oldest_in_queue;
        int idx = (queue_head + offset) % queue_capacity;
        start = (newline_positions[idx] + 1) % TAIL_BUFFER_BYTES;
    }
    if (tail > start) {
        write(STDOUT, tail_buffer + start, tail - start);
    } else if (tail < start) {
        write(STDOUT, tail_buffer + start, TAIL_BUFFER_BYTES - start);
        if (tail > 0) {
            write(STDOUT, tail_buffer, tail);
        }
    }
}

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
        run_stdin_mode(want);
    } else {
        run_file_mode(path, want);
    }
    return 0;
}
