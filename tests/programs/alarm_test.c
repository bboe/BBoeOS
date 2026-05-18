/* alarm_test — consolidated SIGALRM behaviour suite.
 *
 * Dispatches on argv[1] to one of seven modes that were each a
 * separate program before PR #382: cancel, coalesce, default_kill,
 * during_sleep, nesting, oneshot, repeat.  One binary, one bbfs
 * directory slot, identical test coverage. */

int alarm_count;
int got_alarm;
int got_sigint;
int sleep_cf;
char read_buf[4];

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void coalesce_handler(int signum);
void increment_alarm_count(int signum);
void mode_cancel();
void mode_coalesce();
void mode_default_kill();
void mode_during_sleep();
void mode_nesting();
void mode_oneshot();
void mode_repeat();
void nesting_alarm_handler(int signum);
void nesting_sigint_handler(int signum);
int string_equal(char *left, char *right);

/* coalesce needs the handler to busy-wait ~10 ms so the 1 ms repeating
   alarm coalesces pending deliveries; disarms at 8 fires. */
void coalesce_handler(int signum) {
    alarm_count = alarm_count + 1;
    if (alarm_count >= 8) {
        alarm_ms(0, 0);
    }
    int handler_start = uptime_ms();
    while (1) {
        int handler_now = uptime_ms();
        if (handler_now - handler_start >= 10) {
            break;
        }
    }
}

/* Simple counter handler shared by cancel / during_sleep / oneshot /
   repeat — they only care about how many times the alarm fired. */
void increment_alarm_count(int signum) {
    alarm_count = alarm_count + 1;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("alarm_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "cancel")) {
        mode_cancel();
    } else if (string_equal(mode, "coalesce")) {
        mode_coalesce();
    } else if (string_equal(mode, "default_kill")) {
        mode_default_kill();
    } else if (string_equal(mode, "during_sleep")) {
        mode_during_sleep();
    } else if (string_equal(mode, "nesting")) {
        mode_nesting();
    } else if (string_equal(mode, "oneshot")) {
        mode_oneshot();
    } else if (string_equal(mode, "repeat")) {
        mode_repeat();
    } else {
        die("alarm_test: unknown mode\n");
    }
    return 0;
}

void mode_cancel() {
    signal(SIGALRM, increment_alarm_count);
    alarm_ms(50, 0);
    int previous = alarm_ms(0, 0);
    int start = uptime_ms();
    while (1) {
        int now = uptime_ms();
        if (now - start >= 100) {
            break;
        }
    }
    if (alarm_count == 0 && previous >= 40 && previous <= 50) {
        printf("CANCEL_OK prev=%d\n", previous);
    } else {
        printf("CANCEL_BAD count=%d prev=%d\n", alarm_count, previous);
    }
}

void mode_coalesce() {
    signal(SIGALRM, coalesce_handler);
    alarm_ms(1, 1);
    while (alarm_count < 8) {
    }
    if (alarm_count >= 8 && alarm_count <= 12) {
        printf("COALESCE_OK count=%d\n", alarm_count);
    } else {
        printf("COALESCE_BAD count=%d\n", alarm_count);
    }
}

void mode_default_kill() {
    printf("ARMING\n");
    alarm_ms(1, 0);
    while (1) {
        int dummy = uptime_ms();
    }
    printf("UNREACHABLE\n");
}

void mode_during_sleep() {
    signal(SIGALRM, increment_alarm_count);
    int start = uptime_ms();
    alarm_ms(50, 0);
    /* Issue SYS_RTC_SLEEP for 500 ms via inline asm; capture CF into
       sleep_cf (1 = interrupted by alarm, 0 = completed). */
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
        printf("EINTR_BAD count=%d cf=%d elapsed=%d\n", alarm_count, sleep_cf,
               elapsed);
    }
}

void mode_nesting() {
    signal(SIGINT, nesting_sigint_handler);
    signal(SIGALRM, nesting_alarm_handler);
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
}

void mode_oneshot() {
    signal(SIGALRM, increment_alarm_count);
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
}

void mode_repeat() {
    signal(SIGALRM, increment_alarm_count);
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
}

/* nesting installs a SIGINT handler that arms a SIGALRM inside itself
   and busy-waits long enough for the alarm to fire while still inside
   the SIGINT handler (testing the redeliver-on-resume path). */
void nesting_alarm_handler(int signum) {
    got_alarm = 1;
}

void nesting_sigint_handler(int signum) {
    got_sigint = 1;
    alarm_ms(30, 0);
    int handler_start = uptime_ms();
    while (1) {
        int handler_now = uptime_ms();
        if (handler_now - handler_start >= 100) {
            break;
        }
    }
}

int string_equal(char *left, char *right) {
    int index = 0;
    while (left[index] != '\0' && right[index] != '\0') {
        if (left[index] != right[index]) {
            return 0;
        }
        index = index + 1;
    }
    return left[index] == right[index];
}
