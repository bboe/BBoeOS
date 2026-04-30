// serial.c — COM1 serial port driver.
//
// serial_character: AL → COM1 (polled output).  Preserves EAX, EDX —
//     drivers/console.asm calls vga_teletype right after with AL still
//     holding the same character, so AL must round-trip.
//
// Same label (serial_character) and same register-level ABI as the
// original drivers/serial.asm so callers in vga.asm, console.asm, and
// fs/fd/console.asm link unchanged.

#define COM1_DATA 0x3F8
#define COM1_LSR  0x3FD

#define LSR_THR_EMPTY  0x20

// preserve_register names use the 32-bit forms ("eax"/"edx") so cc.py
// emits ``push eax`` / ``pop eax`` rather than the narrower 16-bit
// ``push ax`` (NOTES.md landmine #2).  Asm callers expect the full
// E-registers to survive.
void serial_character(char byte __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("edx")))
{
    while ((kernel_inb(COM1_LSR) & LSR_THR_EMPTY) == 0) {}
    kernel_outb(COM1_DATA, byte);
}
