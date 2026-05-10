/* sleep_forever — block on read indefinitely.  Leaves SIGINT at SIG_DFL
   (default kill) so a Ctrl+C delivered via the serial FIFO kills the
   process via child_terminate, returning wait status 0x0002 (SIGINT).
   expand_dollar_question maps that to bash_status = 128 + 2 = 130. */
int main() {
    char buffer[1];
    while (1) {
        read(STDIN, buffer, 1);
    }
}
