/* alarm_default_kill — arm 1 ms one-shot with no handler installed.
   The kernel must kill the program (SIG_DFL = terminate) and print
   a kill banner.  The shell respawn is the evidence that the kill
   landed cleanly. */

int main() {
    printf("ARMING\n");
    alarm_ms(1, 0);
    while (1) {
        int dummy = uptime_ms();
    }
    printf("UNREACHABLE\n");
    return 0;
}
