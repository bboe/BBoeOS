/* pipe_producer — writes a fixed test pattern to stdout.

   Used by tests/test_pipeline_*.py via the shell as the LHS of a
   pipe.

   Argv subcommands:
     (no arg or unknown) — writes "hello from producer\n" then exits 0
     bulk                — writes 16 KB of '#' then exits 0 (forces
                           multiple ring-buffer fills against any reader)
     early               — writes one byte, exits 7 (status check)
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
    if (argc < 2) {
        write(STDOUT, "hello from producer\n", 20);
        return 0;
    }
    if (strcmp(argv[1], "bulk") == 0) {
        char buffer[64];
        int j = 0;
        while (j < 64) {
            buffer[j] = '#';
            j += 1;
        }
        int i = 0;
        while (i < 256) {
            write(STDOUT, buffer, 64);
            i += 1;
        }
        return 0;
    }
    if (strcmp(argv[1], "early") == 0) {
        write(STDOUT, "x", 1);
        return 7;
    }
    write(STDOUT, "hello from producer\n", 20);
    return 0;
}
