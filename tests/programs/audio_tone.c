/* Smoke test for fd_write_audio: writes ~1.1 second of a 1.1 kHz
 * square wave to /dev/audio.  With QEMU's `-audiodev wav,...`
 * backend, the captured WAV should show energy across the run.
 *
 * Pattern: 5 samples high, 5 samples low, repeat.  At 11025 Hz that's
 * one period per 10 samples ≈ 1102 Hz.
 *
 * 6 × 2048-byte writes = 12288 samples ≈ 1.114 s of audio.  The kernel
 * ring is 2 KB per half so writes must block on IRQ 5 at least 5
 * times (proves the sti+hlt loop wakes correctly).  */

char buf[2048];

int main() {
    int fd = open("/dev/audio", 1);     /* O_WRONLY */
    if (fd < 0) {
        printf("audio_tone: open returned %d (no SB16?)\n", fd);
        return 1;
    }
    int i = 0;
    while (i < 2048) {
        if (((i / 5) & 1) != 0) {
            buf[i] = 200;
        } else {
            buf[i] = 56;
        }
        i = i + 1;
    }
    int chunk = 0;
    while (chunk < 6) {
        printf("audio_tone: write %d start\n", chunk);
        int n = write(fd, buf, 2048);
        printf("audio_tone: write %d returned %d\n", chunk, n);
        chunk = chunk + 1;
    }
    close(fd);
    printf("audio_tone: closed\n");
    return 0;
}
