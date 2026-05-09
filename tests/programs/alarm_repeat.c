/* alarm_repeat — arm 20 ms repeating alarm, count fires for 200 ms,
   assert count is in [8, 12] (allowing IRQ jitter at the boundaries
   — 200/20 = 10, so 8-12 is generous and matches alarm_coalesce). */

int alarm_count;

void on_alarm(int signum) {
    alarm_count = alarm_count + 1;
}

int main() {
    signal(SIGALRM, on_alarm);
    alarm_ms(20, 20);
    int start = uptime_ms();
    while (1) {
        int now = uptime_ms();
        if (now - start >= 200) {
            break;
        }
    }
    alarm_ms(0, 0);
    if (alarm_count >= 8 && alarm_count <= 12) {
        printf("REPEAT_OK count=%d\n", alarm_count);
    } else {
        printf("REPEAT_BAD count=%d\n", alarm_count);
    }
    return 0;
}
