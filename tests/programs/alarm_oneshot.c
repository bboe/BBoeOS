/* alarm_oneshot — install SIGALRM handler, arm 50 ms one-shot,
   busy-wait ~200 ms, assert handler ran exactly once. */

int alarm_count;

void on_alarm(int signum) {
    alarm_count = alarm_count + 1;
}

int main() {
    signal(SIGALRM, on_alarm);
    alarm_ms(50, 0);
    int start = uptime_ms();
    while (1) {
        int now = uptime_ms();
        if (now - start >= 200) {
            break;
        }
    }
    if (alarm_count == 1) {
        printf("ALARM_OK\n");
    } else {
        printf("ALARM_BAD count=%d\n", alarm_count);
    }
    return 0;
}
