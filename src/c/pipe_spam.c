/* pipe_spam — writes 16 KB of '#' to stdout in 64-byte chunks.

   Used by tests/test_pipeline_early_close.py to provoke the producer
   into blocking inside kernel_yield_write after the 4 KB pipe ring
   fills up.  Distinct from `pipe_producer bulk` so the regression
   test has a dedicated, minimal binary whose behaviour cannot drift.
*/

int main() {
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
