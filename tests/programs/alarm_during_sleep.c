/* alarm_during_sleep — install SIGALRM handler, note start time,
   arm a 50 ms one-shot alarm, call SYS_RTC_SLEEP for 500 ms.
   The alarm fires around 50 ms in and short-circuits the sleep with
   CF=1 / AL=ERROR_INTERRUPTED.  Verify: handler ran, CF=1, and
   elapsed time is roughly 50 ms (not the full 500 ms). */

int alarm_count;
int sleep_cf;

void on_alarm(int signum) {
    alarm_count = alarm_count + 1;
}

int main() {
    signal(SIGALRM, on_alarm);
    int start = uptime_ms();
    alarm_ms(50, 0);
    /* Issue SYS_RTC_SLEEP for 500 ms via inline asm; capture CF into
       sleep_cf (1 = interrupted, 0 = completed). */
    asm("mov ecx, 500\n"
        "mov ah, SYS_RTC_SLEEP\n"
        "int 30h\n"
        "setc al\n"
        "movzx eax, al\n"
        "mov [_g_sleep_cf], eax\n");
    int elapsed = uptime_ms() - start;
    if (alarm_count == 1 && sleep_cf == 1 && elapsed >= 40 && elapsed <= 100) {
        printf("EINTR_OK elapsed=%d\n", elapsed);
    } else {
        printf("EINTR_BAD count=%d cf=%d elapsed=%d\n", alarm_count, sleep_cf, elapsed);
    }
    return 0;
}
