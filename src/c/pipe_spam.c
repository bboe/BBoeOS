/* pipe_spam — writes 16 KB of '#' to stdout in 64-byte chunks.

   Used by tests/test_pipeline_early_close.py to provoke the producer
   into blocking inside kernel_yield_write after the 4 KB pipe ring
   fills up.  Distinct from `pipe_producer bulk` so the regression
   test has a dedicated, minimal binary whose behaviour cannot drift.

   Argv subcommands:
     (no arg or unknown) — default SIGPIPE disposition (SIG_DFL): the
                           kernel kills the writer when the reader has
                           closed mid-stream.
     ignore              — install SIG_IGN before writing.  fd_write_pipe
                           still raises pending_sigpipe, but the syscall
                           epilogue clears it and lets write() return -1
                           so this program exits 5 — the SIG_IGN
                           passthrough test distinguishes "EPIPE
                           returned" from "writer killed".
*/

int strcmp(const char *a, const char *b) {
    int index = 0;
    while (1) {
        if (a[index] != b[index]) {
            return a[index] - b[index];
        }
        if (a[index] == '\0') {
            return 0;
        }
        index += 1;
    }
}

int main(int argc, char *argv[]) {
    int ignore_sigpipe = 0;
    if (argc >= 1 && strcmp(argv[0], "ignore") == 0) {
        signal(SIGPIPE, SIG_IGN);
        ignore_sigpipe = 1;
    }
    char buffer[64];
    int j = 0;
    while (j < 64) {
        buffer[j] = '#';
        j += 1;
    }
    int i = 0;
    while (i < 256) {
        int n = write(STDOUT, buffer, 64);
        if (ignore_sigpipe != 0 && n < 0) {
            return 5;
        }
        i += 1;
    }
    return 0;
}
