/* Smoke test for /dev/audio open + IRQ 5 delivery.
 *
 * 1. Open /dev/audio (returns fd >= 0 only if SB16 is present).
 * 2. Sleep ~600 ms via SYS_RTC_SLEEP so the SB16 has time to fire
 *    several half-buffer IRQ 5s (one every ~186 ms).  If the IRQ
 *    handler is broken, this is where we'd see EXCnn or a hang.
 * 3. Close.
 *
 * Pairs with an entry in tests/test_programs.py. */

int main() {
    int fd = open("/dev/audio", 1);     /* O_WRONLY */
    if (fd < 0) {
        printf("audio_open: open returned %d (no SB16?)\n", fd);
        return 1;
    }
    printf("audio_open: fd=%d\n", fd);
    /* Sleep 6 × 100 ms via SYS_RTC_SLEEP (CX = ms). */
    int i = 0;
    while (i < 6) {
        asm("mov cx, 100\n"
            "mov ah, SYS_RTC_SLEEP\n"
            "int 30h\n");
        i = i + 1;
    }
    close(fd);
    printf("audio_open: closed cleanly\n");
    return 0;
}
