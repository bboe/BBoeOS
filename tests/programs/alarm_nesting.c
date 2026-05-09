/* alarm_nesting — install both SIGINT and SIGALRM handlers.  Block
   in read for a queued Ctrl+C (0x03 sent by the test driver via the
   serial FIFO, same trick as sigint_test).  Inside the SIGINT handler,
   arm a 30 ms one-shot SIGALRM and busy-wait 100 ms — the alarm fires
   while still in the SIGINT handler (in_signal_handler blocks immediate
   dispatch), becoming pending.  When the SIGINT handler returns through
   the vDSO trampoline, signal_resume_after_handler's redelivery branch
   dispatches SIGALRM before resuming main.  Main verifies both flags
   are set and prints NESTED_OK. */

int got_alarm;
int got_sigint;
char read_buf[4];

void on_alarm(int signum) {
    got_alarm = 1;
}

void on_sigint(int signum) {
    got_sigint = 1;
    alarm_ms(30, 0);
    int start = uptime_ms();
    while (1) {
        int now = uptime_ms();
        if (now - start >= 100) {
            break;
        }
    }
}

int main() {
    signal(SIGINT, on_sigint);
    signal(SIGALRM, on_alarm);
    asm("mov ebx, 0\n"
        "mov edi, _g_read_buf\n"
        "mov ecx, 1\n"
        "mov ah, SYS_IO_READ\n"
        "int 30h\n");
    if (got_sigint && got_alarm) {
        printf("NESTED_OK\n");
    } else {
        printf("NESTED_BAD sigint=%d alarm=%d\n", got_sigint, got_alarm);
    }
    return 0;
}
