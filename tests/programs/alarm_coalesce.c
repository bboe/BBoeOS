/* alarm_coalesce — 1 ms repeating alarm with a 10 ms handler.
   With a 1 ms alarm interval and a 10 ms handler, each handler
   invocation causes ~10 more alarm ticks while it runs.  Because
   pending_sigalrm is a single bit (not a counter), those 10 ticks
   collapse to a single pending delivery — coalescing.  Without
   coalescing the handler would need to run ~100 times per 100 ms;
   with coalescing it runs ~10 times (once per ~10 ms).

   The handler disarms after 8 fires so the outer loop can run
   after the last handler returns with pending_sigalrm = 0
   (no new alarm ticks since disarm). */

int alarm_count;

void on_alarm(int signum) {
    alarm_count = alarm_count + 1;
    if (alarm_count >= 8) {
        alarm_ms(0, 0);
    }
    int hstart = uptime_ms();
    while (1) {
        int hnow = uptime_ms();
        if (hnow - hstart >= 10) {
            break;
        }
    }
}

int main() {
    signal(SIGALRM, on_alarm);
    alarm_ms(1, 1);
    while (alarm_count < 8) {}
    if (alarm_count >= 8 && alarm_count <= 12) {
        printf("COALESCE_OK count=%d\n", alarm_count);
    } else {
        printf("COALESCE_BAD count=%d\n", alarm_count);
    }
    return 0;
}
