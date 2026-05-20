#ifndef BBOEOS_REGISTERS_H
#define BBOEOS_REGISTERS_H

/*
 * Hardware-register bitfield structs.
 *
 * Bit ordering: LSB-first.  The field declared at offset 0 in each
 * struct occupies bit 0 (the least-significant bit of the underlying
 * byte).  This matches x86 GCC convention and the way most x86
 * datasheets number bits.  If a datasheet draws bits MSB-first (as
 * some do), mentally invert before transcribing here.
 *
 * Every struct here is exactly one byte (containing a bitfield run
 * summing to <= 8 bits) and is meant to bridge to / from a port byte
 * via:
 *
 *     struct foo s = { .field_a = ..., .field_b = ... };
 *     uint8_t *p = (uint8_t *)&s;
 *     kernel_outb(PORT, *p);
 *
 *     uint8_t raw = kernel_inb(PORT);
 *     struct foo *s = (struct foo *)&raw;
 *     ... s->field ...;
 *
 * The struct-literal write path benefits from designated-init +
 * bitfield-collapse peepholes: a multi-field init folds to a single
 * ``mov byte [ebp-K], <const>``.  The read path stays as a pointer
 * cast because cc.py doesn't parse ``*(T *)expr``.
 *
 * Structs are listed alphabetically by tag.
 */

/* NE2000 / DP8390 command register (offset 0x00, both pages).
 *
 *  stop:     1 = stop NIC
 *  start:    1 = start NIC
 *  transmit: 1 = start packet transmission
 *  rd[3]:    remote DMA command (000 = not allowed, 001 = read,
 *            010 = write, 011 = send packet, 100 = abort/complete)
 *  page[2]:  register page select (00 = page 0, 01 = page 1, 10 = page 2)
 */
struct ne2k_cr {
    uint8_t stop : 1;
    uint8_t start : 1;
    uint8_t transmit : 1;
    uint8_t rd : 3;
    uint8_t page : 2;
};

/* Data configuration register (offset 0x0E, page 0). */
struct ne2k_dcr {
    uint8_t wts : 1; /* word transfer select (1 = 16-bit) */
    uint8_t bos : 1; /* byte order select (0 = little-endian) */
    uint8_t las : 1; /* long address select (0 = 16-bit DMA) */
    uint8_t ls : 1;  /* loopback select (0 = normal) */
    uint8_t arm : 1; /* auto-init remote (0 = disabled) */
    uint8_t ft : 2;  /* FIFO threshold */
    uint8_t : 1;
};

/* NE2000 interrupt mask register (offset 0x0F, page 0).
 *
 *  Bit layout mirrors ISR: 1 = enabled, 0 = masked.  Bit 7 is reserved.
 */
struct ne2k_imr {
    uint8_t prx : 1;
    uint8_t ptx : 1;
    uint8_t rxe : 1;
    uint8_t txe : 1;
    uint8_t ovw : 1;
    uint8_t cnt : 1;
    uint8_t rdc : 1;
    uint8_t : 1;
};

/* NE2000 interrupt status register (offset 0x07, page 0).
 *
 *  Writing a 1 to a bit acks it.  ``rst`` is read-only.
 */
struct ne2k_isr {
    uint8_t prx : 1; /* packet received OK */
    uint8_t ptx : 1; /* packet transmitted OK */
    uint8_t rxe : 1; /* receive error */
    uint8_t txe : 1; /* transmit error */
    uint8_t ovw : 1; /* RX-ring overwrite warning */
    uint8_t cnt : 1; /* counter overflow */
    uint8_t rdc : 1; /* remote DMA complete */
    uint8_t rst : 1; /* reset status */
};

/* Receive configuration register (offset 0x0C, page 0). */
struct ne2k_rcr {
    uint8_t sep : 1; /* save errored packets */
    uint8_t ar : 1;  /* accept runt packets */
    uint8_t ab : 1;  /* accept broadcast */
    uint8_t am : 1;  /* accept multicast */
    uint8_t pro : 1; /* promiscuous physical */
    uint8_t mon : 1; /* monitor mode (no RX) */
    uint8_t : 2;
};

/* Transmit configuration register (offset 0x0D, page 0). */
struct ne2k_tcr {
    uint8_t crc : 1;  /* inhibit CRC */
    uint8_t lb : 2;   /* loopback control */
    uint8_t atd : 1;  /* auto-transmit disable */
    uint8_t ofst : 1; /* collision-offset enable */
    uint8_t : 3;
};

/* 8259A interrupt-mask register.  Bit N == 1 disables IRQ N.
 *
 * The PIC1 IMR is at port 0x21 and covers IRQs 0..7.  The PIC2 IMR
 * is at port 0xA1 and covers IRQs 8..15; the same struct works for
 * both since the bit layout is identical (the field names refer to
 * "bit N" of whichever IMR you're touching, not absolute IRQ
 * numbers).
 */
struct pic_imr {
    uint8_t irq0 : 1;
    uint8_t irq1 : 1;
    uint8_t irq2 : 1;
    uint8_t irq3 : 1;
    uint8_t irq4 : 1;
    uint8_t irq5 : 1;
    uint8_t irq6 : 1;
    uint8_t irq7 : 1;
};

#endif
