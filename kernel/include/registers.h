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

/* ATA device-control register (port 0x3F6, write-side).
 *
 *  nien:     1 = block IRQs from the controller
 *  srst:     1 = hold the controller in software reset (clear, wait,
 *            then re-clear to release)
 *  hob:      1 = "high-order byte" select for LBA48 reads
 *
 * Bit 0 is always 0; bits 3..5 are reserved.
 */
struct ata_dcr {
    uint8_t : 1;
    uint8_t nien : 1;
    uint8_t srst : 1;
    uint8_t : 4;
    uint8_t hob : 1;
};

/* ATA drive/head register (port 0x1F6).
 *
 *  lba_high[4]: in LBA mode, bits 24..27 of the LBA; in CHS mode, the
 *               head number (0..15)
 *  slave:       0 = master device, 1 = slave
 *  reserved_5:  must be 1 (legacy / obsolete)
 *  lba:         1 = LBA addressing, 0 = CHS
 *  reserved_7:  must be 1 (legacy / obsolete)
 *
 * The two reserved-must-be-1 bits are what makes a "master + LBA + head 0"
 * write come out as 0xE0 rather than 0x40.
 */
struct ata_drive_head {
    uint8_t lba_high : 4;
    uint8_t slave : 1;
    uint8_t reserved_5 : 1;
    uint8_t lba : 1;
    uint8_t reserved_7 : 1;
};

/* ATA status register (port 0x1F7, read-side).
 *
 *  err:  command ended with an error; details in the error register
 *  idx:  index pulse (legacy, ignored)
 *  corr: data corrected via ECC (legacy)
 *  drq:  data request — controller has a sector ready (read) or is
 *        ready to accept one (write)
 *  dsc:  drive seek complete (also called SRV on packet devices)
 *  df:   drive fault (write-fault on legacy drives)
 *  rdy:  drive ready to accept commands
 *  bsy:  controller busy; ignore every other bit while this is 1
 */
struct ata_status {
    uint8_t err : 1;
    uint8_t idx : 1;
    uint8_t corr : 1;
    uint8_t drq : 1;
    uint8_t dsc : 1;
    uint8_t df : 1;
    uint8_t rdy : 1;
    uint8_t bsy : 1;
};

/* 8237A single-channel mask register (port 0x0A for ch 0-3,
 * port 0xD4 for ch 4-7).  Writing here masks or unmasks one DMA
 * channel without disturbing the other three.
 *
 *  channel[2]: 00 = ch0/4, 01 = ch1/5, 10 = ch2/6, 11 = ch3/7
 *  set:        1 = mask the channel, 0 = unmask
 *  reserved:   bits 7-3
 */
struct dma_mask {
    uint8_t channel : 2;
    uint8_t set : 1;
    uint8_t : 5;
};

/* 8237A mode register (port 0x0B for ch 0-3, port 0xD6 for ch 4-7).
 *
 *  channel[2]:  DMA channel within the controller
 *  transfer[2]: 00 = verify, 01 = write (peripheral -> mem),
 *               10 = read (mem -> peripheral), 11 = illegal
 *  autoinit:    1 = reload address/count from base regs at TC
 *  decrement:   0 = address increment, 1 = decrement
 *  mode[2]:     00 = demand, 01 = single, 10 = block, 11 = cascade
 */
struct dma_mode {
    uint8_t channel : 2;
    uint8_t transfer : 2;
    uint8_t autoinit : 1;
    uint8_t decrement : 1;
    uint8_t mode : 2;
};

/* 82077AA / 8272 digital output register (port 0x3F2).
 *
 *  drive[2]:   selected drive 0..3
 *  reset_not:  0 = hold controller in reset, 1 = run
 *  dma_irq:    1 = enable DMA/IRQ pin (required for any normal use)
 *  motor_*:    1 = spin drive N's motor
 */
struct fdc_dor {
    uint8_t drive : 2;
    uint8_t reset_not : 1;
    uint8_t dma_irq : 1;
    uint8_t motor_0 : 1;
    uint8_t motor_1 : 1;
    uint8_t motor_2 : 1;
    uint8_t motor_3 : 1;
};

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
