// opl3.c — Sound Blaster 16 built-in OPL3 FM chip driver.
//
// Caller-facing surface (referenced by fs/fd/midi.c, drivers/sb16.c):
//
//     opl3_present (uint8_t) — set to 1 by sb16_init when the SB16 DSP
//                              probe succeeded (real SB16 cards always
//                              include OPL3, so the SB16 probe doubles
//                              as the OPL3 probe); read from fd_open's
//                              /dev/midi branch and MIDI_IOCTL_QUERY.
//                              See sb16_init's comment block for the
//                              detection-sequence rationale.
//     opl_write (bank, reg, value) — outb status, iodelay, outb data.
//                                    Trusts inputs (caller validates
//                                    bank ∈ {0, 1}).
//     opl_silence_all () — emit KEY_OFF (clear bit 5 of regs 0xB0..0xB8)
//                          on both banks; safe to call from IRQ 0
//                          context or fd_open / fd_close paths.
//
// Wire-port layout (matches AdLib / SB16 / OPL3 standard):
//     bank 0: status @ 0x388, data @ 0x389
//     bank 1: status @ 0x38A, data @ 0x38B  (OPL3-only second array)
//
// Inline asm rationale matches drivers/sb16.c: cc.py's prologue/epilogue
// would otherwise clobber the register-state contract some callers
// (the IRQ 0 ISR draining the midi queue) need to honour.

uint8_t opl3_present;
asm("opl3_present equ _g_opl3_present");

// Forward declaration so opl_silence_all (alphabetically earlier) can
// call opl_write below.  See sb16.c lines 65-73 for the matching cc.py
// pattern.
void opl_write(int bank, int reg, int value);

// io_delay_short — ~1 µs delay after writing the OPL status port,
// implemented as four reads of the unused POST diagnostic port 0x80
// (same pattern as sb16_reset_delay).  OPL chips need a register-
// settling delay between status and data writes; on real hardware this
// is ~3.3 µs, on QEMU it's a no-op but the reads are cheap.
void io_delay_short() {
    kernel_inb(0x80);
    kernel_inb(0x80);
    kernel_inb(0x80);
    kernel_inb(0x80);
}

// opl_silence_all: KEY_OFF for the 9 voices on each bank.  Reg 0xB0..0xB8
// is the "key-on / block / F-number-high" register; clearing bit 5
// silences the voice without resetting block or F-number, so a
// subsequent KEY_ON on the same voice resumes immediately.
void opl_silence_all() {
    int voice;
    voice = 0;
    while (voice < 9) {
        opl_write(0, 0xB0 + voice, 0);
        opl_write(1, 0xB0 + voice, 0);
        voice = voice + 1;
    }
}

// opl_write: outb status, settle, outb data.  No bounds check on bank;
// caller (fd_write_midi) drops bank > 1 before calling.
void opl_write(int bank, int reg, int value) {
    int status_port;
    int data_port;
    if (bank == 0) {
        status_port = 0x388;
        data_port = 0x389;
    } else {
        status_port = 0x38A;
        data_port = 0x38B;
    }
    kernel_outb(status_port, reg);
    io_delay_short();
    kernel_outb(data_port, value);
    io_delay_short();
}
