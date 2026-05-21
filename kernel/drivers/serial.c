#include "types.h"

// serial.c — COM1 serial port driver (polled TX, IRQ 4 RX).
//
// Output:
//   serial_character: AL → COM1 (polled).  Preserves EAX, EDX —
//     drivers/console.asm calls vga_teletype right after with AL still
//     holding the same character, so AL must round-trip.
//
// Input:
//   serial_init   — install pmode_irq4_handler at IDT 0x24, enable
//                   UART IER bit 0 (received-data-ready), unmask IRQ 4
//                   on PIC1.  Called once from entry.asm before sti
//                   ordering would matter (drivers init runs with IF=1
//                   so a byte queued between IER-enable and PIC unmask
//                   is held in PIC IRR and delivered after unmask).
//   serial_getc   — pop one byte from serial_ring; returns 0 (with ZF
//                   set) if empty.  Mirrors ps2_getc's contract so
//                   fd_read_console can treat serial and PS/2 ring
//                   reads identically.
//   pmode_irq4_handler (in entry.asm) — reads 0x3F8 (which also clears
//                   the UART RX interrupt), calls serial_putc to push
//                   the byte into serial_ring, EOIs PIC1, iretds.
//
// Same label (serial_character) and same register-level ABI as the
// original drivers/serial.asm so callers in vga.asm, console.asm, and
// fs/fd/console.asm link unchanged.

#define COM1_DATA 0x3F8
#define COM1_IER 0x3F9
#define COM1_LSR 0x3FD

#define IER_RX_READY 0x01
#define LSR_THR_EMPTY 0x20

// Ring buffer for IRQ-pushed RX bytes.  Power-of-two size so head/tail
// wrap with a cheap AND.  128 leaves comfortable headroom for line-at-
// a-time bursts from the test driver (a single host-side write can hit
// the UART FIFO in one batch, and the IRQ handler drains the whole
// FIFO before returning) — interactive typing fits in any size.
#define SERIAL_RING_SIZE 128

u8 serial_buf[SERIAL_RING_SIZE];
u8 serial_head;
u8 serial_tail;

// Bare-name aliases so the asm IRQ stub in entry.asm can reach them
// without the _g_ prefix cc.py emits for C globals.
asm("serial_buf equ _g_serial_buf");
asm("serial_head equ _g_serial_head");
asm("serial_tail equ _g_serial_tail");

// preserve_register names use the 32-bit forms ("eax"/"edx") so cc.py
// emits ``push eax`` / ``pop eax`` rather than the narrower 16-bit
// ``push ax`` (NOTES.md landmine #2).  Asm callers expect the full
// E-registers to survive.
void serial_character(char byte __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("edx"))) {
    while ((kernel_inb(COM1_LSR) & LSR_THR_EMPTY) == 0) {
    }
    kernel_outb(COM1_DATA, byte);
}

// serial_getc: pop one byte from serial_ring.  Returns AL = byte and
// ZF clear on success, AL = 0 and ZF set if empty.  Mirrors ps2_getc.
// Implemented in asm so the read of the IRQ-shared tail uses a single
// load — on a single CPU the IRQ never touches head and the process
// side never touches tail, so no bracketing is needed.
char serial_getc();

asm("serial_getc:\n"
    "    push ebx\n"
    "    movzx ebx, byte [serial_head]\n"
    "    mov al, [serial_tail]\n"
    "    cmp bl, al\n"
    "    je .serial_getc_empty\n"
    "    mov al, [serial_buf + ebx]\n"
    "    inc bl\n"
    "    and bl, 0x7F\n" // SERIAL_RING_SIZE - 1
    "    mov [serial_head], bl\n"
    "    test al, al\n" // set ZF based on byte value
    "    pop ebx\n"
    "    ret\n"
    ".serial_getc_empty:\n"
    "    xor eax, eax\n"
    "    pop ebx\n"
    "    ret\n");

// serial_init: enable UART RX interrupt, install IDT 0x24 handler,
// unmask IRQ 4 on PIC1.  Called once from protected_mode_entry.
void serial_init();

asm("serial_init:\n"
    "    push eax\n"
    "    push ebx\n"
    "    push edx\n"
    // Install pmode_irq4_handler at vector 0x24 first; any pending
    // edge that lands before we unmask the PIC will be held in IRR
    // and delivered when we unmask below.
    "    mov eax, pmode_irq4_handler\n"
    "    mov bl, 0x24\n"
    "    call idt_set_gate32\n"
    // UART register ports are all > 0xFF, so the immediate `in/out
    // al, port` form silently truncates the constant to 8 bits
    // (would land on 0xF8..0xFD instead of 0x3F8..0x3FD).  Load DX
    // and use the indirect form throughout, mirroring the SB16
    // driver's note in entry.asm.
    // Drain any byte already sitting in the UART RX register so we
    // start from a clean slate (and so the first real keypress
    // generates a fresh edge after IER goes on).
    "    mov dx, 0x3FD\n" // LSR
    "    in al, dx\n"
    "    test al, 0x01\n"
    "    jz .serial_init_no_pending\n"
    "    mov dx, 0x3F8\n" // DATA
    "    in al, dx\n"
    ".serial_init_no_pending:\n"
    // Force DLAB=0 so port 0x3F9 is IER, not the divisor latch high
    // byte.  Set 8N1 explicitly while we're touching LCR.
    "    mov dx, 0x3FB\n" // LCR
    "    mov al, 0x03\n"  // 8N1, DLAB=0
    "    out dx, al\n"
    // MCR bit 3 (OUT2 = 0x08) gates the UART's IRQ output onto the
    // ISA IRQ 4 line — without it the UART asserts INTR internally
    // but the PIC never sees the edge.  Bit 1 (RTS = 0x02) and bit 0
    // (DTR = 0x01) are tradition; harmless on QEMU and expected by
    // real terminals that look for them.
    "    mov dx, 0x3FC\n" // MCR
    "    mov al, 0x0B\n"  // DTR | RTS | OUT2
    "    out dx, al\n"
    // Enable received-data-ready interrupt.
    "    mov dx, 0x3F9\n" // IER
    "    mov al, 0x01\n"
    "    out dx, al\n"
    // Unmask IRQ 4 on PIC1.
    "    in al, 0x21\n"
    "    and al, 0xEF\n" // clear bit 4
    "    out 0x21, al\n"
    "    pop edx\n"
    "    pop ebx\n"
    "    pop eax\n"
    "    ret\n");

// serial_putc: push one byte into serial_ring; drop silently when
// full.  Called only from the IRQ 4 handler path, so head/tail
// concurrency is one-sided (IRQ writes tail; reader writes head).
void serial_putc(char byte __attribute__((in_register("ax")))) {
    u8 tail;
    u8 next;
    tail = serial_tail;
    next = (tail + 1) & (SERIAL_RING_SIZE - 1);
    if (next == serial_head) {
        return;
    }
    serial_buf[tail] = byte;
    serial_tail = next;
}
