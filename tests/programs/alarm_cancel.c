/* alarm_cancel — arm 50 ms one-shot, immediately cancel via
   alarm_ms(0,0), busy-wait 100 ms, assert handler did NOT run.
   The return value from the cancel call is the ms remaining on the
   prior alarm (approximately 50). */

int alarm_count;

void on_alarm(int signum) {
    alarm_count = alarm_count + 1;
}

int main() {
    signal(SIGALRM, on_alarm);
    alarm_ms(50, 0);
    int prev = alarm_ms(0, 0);
    int start = uptime_ms();
    while (1) {
        int now = uptime_ms();
        if (now - start >= 100) {
            break;
        }
    }
    if (alarm_count == 0 && prev >= 40 && prev <= 50) {
        printf("CANCEL_OK prev=%d\n", prev);
    } else {
        printf("CANCEL_BAD count=%d prev=%d\n", alarm_count, prev);
    }
    return 0;
}
