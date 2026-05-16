/* audio_test — consolidated /dev/audio and /dev/midi smoke tests.
 *
 * Three modes, each previously its own program:
 *
 *   open    — open /dev/audio, sleep ~600 ms while SB16 fires several
 *             half-buffer IRQ 5s, close.  Catches a broken IRQ 5
 *             handler (hangs / EXCnn).  Previously audio_open.c.
 *
 *   tone    — write 6 × 2048-byte chunks of a 1.1 kHz square wave
 *             through fd_write_audio.  Exercises the sti+hlt blocking
 *             loop; each write must return 2048 once the ring drains.
 *             Previously audio_tone.c.
 *
 *   midi    — open /dev/midi, queue a 1 s A4 tone on OPL voice 0,
 *             drain the queue (MIDI_IOCTL_DRAIN) so the delayed
 *             KEY_OFF actually fires, close.  Previously play_midi.c.
 *
 * The midi mode's inline asm references `_g_midi_fd` at file scope —
 * cc.py's userland inline-asm can't see locals. */

char audio_buffer[2048];
char midi_command[6];
int midi_fd;

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void midi_emit(int delay, int bank, int register_index, int value);
void mode_midi();
void mode_open();
void mode_tone();
int string_equal(char *left, char *right);

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("audio_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "open")) {
        mode_open();
    } else if (string_equal(mode, "tone")) {
        mode_tone();
    } else if (string_equal(mode, "midi")) {
        mode_midi();
    } else {
        die("audio_test: unknown mode\n");
    }
    return 0;
}

void midi_emit(int delay, int bank, int register_index, int value) {
    midi_command[0] = delay & 0xFF;
    midi_command[1] = (delay >> 8) & 0xFF;
    midi_command[2] = bank & 0xFF;
    midi_command[3] = register_index & 0xFF;
    midi_command[4] = value & 0xFF;
    midi_command[5] = 0;
    write(midi_fd, midi_command, 6);
}

void mode_midi() {
    midi_fd = open("/dev/midi", 1);                 /* O_WRONLY */
    if (midi_fd < 0) {
        printf("play_midi: open returned %d (no OPL3?)\n", midi_fd);
        return;
    }

    /* Operator 0 (carrier-side under additive C0=01): mult=1, sustained. */
    midi_emit(0, 0, 0x20, 0x01);
    midi_emit(0, 0, 0x40, 0x10);        /* total level: medium-loud */
    midi_emit(0, 0, 0x60, 0xF0);        /* fast attack, slow decay */
    midi_emit(0, 0, 0x80, 0x77);        /* max sustain, fast release */

    /* Operator 1 (modulator slot, simple patch). */
    midi_emit(0, 0, 0x23, 0x01);
    midi_emit(0, 0, 0x43, 0x00);        /* max output */
    midi_emit(0, 0, 0x63, 0xF0);
    midi_emit(0, 0, 0x83, 0x77);

    /* Voice 0 connection: additive (0x01 selects additive). */
    midi_emit(0, 0, 0xC0, 0x01);

    /* F-number low byte for A4 at block 4 (F-num ~= 0x2AE). */
    midi_emit(0, 0, 0xA0, 0xAE);

    /* KEY_ON | block=4 | F-num high bits (0x2AE >> 8 = 0x02). */
    midi_emit(0, 0, 0xB0, 0x32);

    /* Hold for 1 s, then KEY_OFF (same block + F-num, KEY_ON cleared). */
    midi_emit(1000, 0, 0xB0, 0x12);

    /* MIDI_IOCTL_DRAIN: block until the kernel queue empties so the
       1 s-delayed KEY_OFF fires before close() drops it. */
    asm("mov bx, [_g_midi_fd]\n"
        "mov al, MIDI_IOCTL_DRAIN\n"
        "mov ah, SYS_IO_IOCTL\n"
        "int 30h\n");

    close(midi_fd);
    printf("play_midi: done\n");
}

void mode_open() {
    int fd = open("/dev/audio", 1);                 /* O_WRONLY */
    if (fd < 0) {
        printf("audio_open: open returned %d (no SB16?)\n", fd);
        return;
    }
    printf("audio_open: fd=%d\n", fd);
    /* Sleep 6 x 100 ms via SYS_RTC_SLEEP (CX = ms). */
    int i = 0;
    while (i < 6) {
        asm("mov cx, 100\n"
            "mov ah, SYS_RTC_SLEEP\n"
            "int 30h\n");
        i = i + 1;
    }
    close(fd);
    printf("audio_open: closed cleanly\n");
}

void mode_tone() {
    int fd = open("/dev/audio", 1);                 /* O_WRONLY */
    if (fd < 0) {
        printf("audio_tone: open returned %d (no SB16?)\n", fd);
        return;
    }
    int i = 0;
    while (i < 2048) {
        if (((i / 5) & 1) != 0) {
            audio_buffer[i] = 200;
        } else {
            audio_buffer[i] = 56;
        }
        i = i + 1;
    }
    int chunk = 0;
    while (chunk < 6) {
        printf("audio_tone: write %d start\n", chunk);
        int n = write(fd, audio_buffer, 2048);
        printf("audio_tone: write %d returned %d\n", chunk, n);
        chunk = chunk + 1;
    }
    close(fd);
    printf("audio_tone: closed\n");
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
