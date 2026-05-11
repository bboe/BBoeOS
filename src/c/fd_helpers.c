/* fd_helpers — multi-subcommand test program for fd-related kernel
   features (dirty bit, dup, dup2, fd-table inheritance on exec).

   Consolidates several small test programs into one bin/ entry to stay
   under the bbfs 48-entry per-directory cap when --with-test-programs
   adds the tests/programs/ fixtures.

   Usage: fd_helpers <case> [args...]

   Cases:
     dup_console          dup(1) → expect fd >= 3; write "dup_ok\n"; close.
     noop <path>          open(path, O_WRONLY); close(fd) — exercises the
                          dirty-bit gate.  No write happens, so close must
                          not flush a stale position over the file's size. */

int run_dup_console() {
    int new_fd = dup(1);
    if (new_fd < 3) {
        die("dup returned unexpected fd\n");
    }
    write(new_fd, "dup_ok\n", 7);
    close(new_fd);
    return 0;
}

int run_noop(char *path) {
    int fd = open(path, O_WRONLY);
    if (fd < 0) {
        die("open failed\n");
    }
    close(fd);
    return 0;
}

int strcmp(const char *a, const char *b) {
    int index = 0;
    while (1) {
        if (a[index] != b[index]) {
            return a[index] - b[index];
        }
        if (a[index] == '\0') {
            return 0;
        }
        index = index + 1;
    }
}

int main(int argc, char *argv[]) {
    if (argc < 1) {
        die("Usage: fd_helpers <case> [args]\n");
    }
    /* argv[0] holds the full arg tail past the program name; split at
       the first space so the case name is null-terminated and the
       remainder is the arg string for that case. */
    char *case_name = argv[0];
    int split = 0;
    while (case_name[split] != '\0' && case_name[split] != ' ') {
        split = split + 1;
    }
    char *rest = case_name + split;
    if (case_name[split] == ' ') {
        case_name[split] = '\0';
        rest = case_name + split + 1;
    }
    if (strcmp(case_name, "dup_console") == 0) {
        return run_dup_console();
    }
    if (strcmp(case_name, "noop") == 0) {
        if (rest[0] == '\0') {
            die("Usage: fd_helpers noop <path>\n");
        }
        return run_noop(rest);
    }
    die("unknown case\n");
    return 1;
}
