/* pipe_spam_ignore — like pipe_spam but installs SIG_IGN for SIGPIPE.

   When the consumer closes the read end mid-stream, fd_write_pipe
   raises pending_sigpipe.  With the handler set to SIG_IGN, the
   syscall epilogue clears the pending bit and lets write() return -1
   to userspace.  This program checks for that -1 return and exits 5
   so the test can distinguish "EPIPE returned" from "writer killed."
*/

int main() {
    signal(SIGPIPE, SIG_IGN);
    char buffer[64];
    int j = 0;
    while (j < 64) {
        buffer[j] = '#';
        j += 1;
    }
    int i = 0;
    while (i < 256) {
        int n = write(STDOUT, buffer, 64);
        if (n < 0) {
            return 5;
        }
        i += 1;
    }
    return 0;
}
