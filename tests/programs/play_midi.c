/* play_midi -- smoke test for /dev/midi.  Plays A4 (~440 Hz) on OPL voice 0
 * for ~1 s, then KEY_OFFs and exits.  Does not depend on Doom or any WAD.
 *
 * The wire format on /dev/midi is 6 bytes per command:
 *   byte 0: delay low (0..255 ms)
 *   byte 1: delay high (256..65535 ms; usually 0)
 *   byte 2: bank (0 or 1)
 *   byte 3: register
 *   byte 4: value
 *   byte 5: reserved (0)
 *
 * All voice-setup commands run with delay=0 (immediate); the final
 * KEY_OFF carries delay=1000 ms so the kernel queue holds the note
 * for one second before releasing it.  Before close(), we issue a
 * MIDI_IOCTL_DRAIN so the kernel queue actually empties to the chip
 * — fd_close_midi drops pending events, so a fast close would lose
 * the delayed KEY_OFF (and any setup writes that hadn't drained yet
 * because no IRQ fired during play_midi's microsecond-scale run). */

char command[6];

/* fd at file scope so the inline-asm DRAIN syscall can reach it via
 * its `_g_fd` symbol — cc.py userland inline-asm can't see locals. */
int fd;

void emit(int delay, int bank, int reg, int value) {
    command[0] = delay & 0xFF;
    command[1] = (delay >> 8) & 0xFF;
    command[2] = bank & 0xFF;
    command[3] = reg & 0xFF;
    command[4] = value & 0xFF;
    command[5] = 0;
    write(fd, command, 6);
}

int main() {
    fd = open("/dev/midi", 1);              /* O_WRONLY */
    if (fd < 0) {
        printf("play_midi: open returned %d (no OPL3?)\n", fd);
        return 1;
    }

    /* Operator 0 (carrier-side under additive C0=01): mult=1, sustained. */
    emit(0, 0, 0x20, 0x01);
    emit(0, 0, 0x40, 0x10);     /* total level: medium-loud */
    emit(0, 0, 0x60, 0xF0);     /* fast attack, slow decay */
    emit(0, 0, 0x80, 0x77);     /* max sustain, fast release */

    /* Operator 1 (modulator slot, simple patch). */
    emit(0, 0, 0x23, 0x01);
    emit(0, 0, 0x43, 0x00);     /* max output */
    emit(0, 0, 0x63, 0xF0);
    emit(0, 0, 0x83, 0x77);

    /* Voice 0 connection: feedback=0, additive (FM bit = 1 on OPL means
     * additive in the connection bit; 0x01 selects additive). */
    emit(0, 0, 0xC0, 0x01);

    /* F-number low byte for A4 at block 4 (F-num ~= 0x2AE). */
    emit(0, 0, 0xA0, 0xAE);

    /* KEY_ON | block=4 | F-num high bits (0x2AE >> 8 = 0x02). */
    emit(0, 0, 0xB0, 0x32);

    /* Hold for 1 s, then KEY_OFF (same block + F-num, KEY_ON bit cleared). */
    emit(1000, 0, 0xB0, 0x12);

    /* MIDI_IOCTL_DRAIN: block until the kernel queue empties, so the
     * 1 s-delayed KEY_OFF actually fires before close() drops it. */
    asm("mov bx, [_g_fd]\n"
        "mov al, MIDI_IOCTL_DRAIN\n"
        "mov ah, SYS_IO_IOCTL\n"
        "int 30h\n");

    close(fd);
    printf("play_midi: done\n");
    return 0;
}
