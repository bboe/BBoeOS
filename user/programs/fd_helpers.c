/* fd_helpers — multi-subcommand test program for fd-related kernel
   features (dirty bit, dup, dup2, fd-table inheritance on exec).

   Consolidates several small test programs into one bin/ entry to stay
   under the bbfs 48-entry per-directory cap when --with-test-programs
   adds the tests/programs/ fixtures.

   Usage: fd_helpers <case> [args...]

   Cases:
     dup_console          dup(1) → expect fd >= 3; write "dup_ok\n"; close.
     dup_vga              dup(/dev/vga) must be refused (singleton-opener).
     dup2_close_target    dup(1)→a; dup2(1,a)→a; write through a.
     dup2_self            dup2(N,N) is a no-op and returns N.
     noop <path>          open(path, O_WRONLY); close(fd) — exercises the
                          dirty-bit gate.  No write happens, so close must
                          not flush a stale position over the file's size. */

int strcmp(const char *a, const char *b);

int run_dup2_close_target() {
    /* dup(1) → some fd >= 3; then dup2(1, that_fd) to force the close-and-overwrite
       path; verify we can still write through the target fd afterwards. */
    int a = dup(1);
    if (a < 3) {
        die("dup\n");
    }
    int b = dup2(1, a);
    if (b != a) {
        die("dup2 target\n");
    }
    write(a, "dup2_close_ok\n", 14);
    return 0;
}

int run_dup2_self() {
    /* dup2(N, N) must be a no-op and return N (Linux semantics). */
    int got = dup2(1, 1);
    if (got != 1) {
        die("dup2(N,N) must return N\n");
    }
    write(1, "dup2_self_ok\n", 13);
    return 0;
}

int run_dup_console() {
    int new_fd = dup(1);
    if (new_fd < 3) {
        die("dup returned unexpected fd\n");
    }
    write(new_fd, "dup_ok\n", 7);
    close(new_fd);
    return 0;
}

int run_dup_vga() {
    /* dup of /dev/vga must refuse (singleton-opener). */
    int vga_fd = open("/dev/vga", O_WRONLY);
    if (vga_fd < 0) {
        die("open vga\n");
    }
    int dup_result = dup(vga_fd);
    if (dup_result >= 0) {
        die("dup of vga must fail\n");
    }
    write(1, "dup_vga_refused\n", 16);
    close(vga_fd);
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

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("Usage: fd_helpers <case> [args]\n");
    }
    /* Linux-style argv: argv[0]=basename, argv[1]=case_name,
       argv[2]=optional path for cases like `noop`. */
    char *case_name = argv[1];
    if (strcmp(case_name, "dup2_close_target") == 0) {
        return run_dup2_close_target();
    }
    if (strcmp(case_name, "dup2_self") == 0) {
        return run_dup2_self();
    }
    if (strcmp(case_name, "dup_console") == 0) {
        return run_dup_console();
    }
    if (strcmp(case_name, "dup_vga") == 0) {
        return run_dup_vga();
    }
    if (strcmp(case_name, "noop") == 0) {
        if (argc < 3) {
            die("Usage: fd_helpers noop <path>\n");
        }
        return run_noop(argv[2]);
    }
    die("unknown case\n");
    return 1;
}
