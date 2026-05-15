/* line_helpers.h — single-fd line bufferfer for stream-oriented filters.

   read_line reads up to and including the next '\n' from fd into buffer.
   - Returns >0 = bytes written (no trailing NUL).
   - Returns 0 on EOF when nothing was read since the last call.
   - Returns -1 on read error.

   If a line exceeds max - 1 bytes, the tail is dropped until the next
   '\n' (or EOF) and the function returns max - 1. This keeps callers
   like wc and grep correct on count / match without per-line allocation.
   MAX_LINE = 1024 is sized for stack-allocated buffer[MAX_LINE]. */

#ifndef LINE_HELPERS_H
#define LINE_HELPERS_H

#define MAX_LINE 1024

int read_line(int fd, char *buffer, int max) {
    int count = 0;
    while (count < max - 1) {
        int bytes_read = read(fd, buffer + count, 1);
        if (bytes_read < 0) {
            return -1;
        }
        if (bytes_read == 0) {
            return count;
        }
        if (buffer[count] == '\n') {
            count += 1;
            return count;
        }
        count += 1;
    }
    /* Buffer full without finding '\n'; drain into the unused slot at
       buffer[max - 1] (which the caller never reads — return value caps
       at count == max - 1) until we hit a newline or EOF. */
    while (1) {
        int bytes_read = read(fd, buffer + max - 1, 1);
        if (bytes_read <= 0) {
            return count;
        }
        if (buffer[max - 1] == '\n') {
            return count;
        }
    }
}

#endif
