#ifndef BBOEOS_REGISTERS_H
#define BBOEOS_REGISTERS_H

#include "types.h"

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
 *     u8 *p = (u8 *)&s;
 *     kernel_outb(PORT, *p);
 *
 *     u8 raw = kernel_inb(PORT);
 *     ... ((struct foo *)&raw)->field ...;
 *
 * The struct-literal write path benefits from designated-init +
 * bitfield-collapse peepholes: a multi-field init folds to a single
 * ``mov byte [ebp-K], <const>``.  The read path drops the named-pointer
 * local entirely now that cc.py accepts ``((struct T *)expr)->field`` as
 * a single postfix expression.
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
    u8 : 1;
    u8 nien : 1;
    u8 srst : 1;
    u8 : 4;
    u8 hob : 1;
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
    u8 lba_high : 4;
    u8 slave : 1;
    u8 reserved_5 : 1;
    u8 lba : 1;
    u8 reserved_7 : 1;
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
    u8 err : 1;
    u8 idx : 1;
    u8 corr : 1;
    u8 drq : 1;
    u8 dsc : 1;
    u8 df : 1;
    u8 rdy : 1;
    u8 bsy : 1;
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
    u8 channel : 2;
    u8 set : 1;
    u8 : 5;
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
    u8 channel : 2;
    u8 transfer : 2;
    u8 autoinit : 1;
    u8 decrement : 1;
    u8 mode : 2;
};

/* 82077AA / 8272 digital output register (port 0x3F2).
 *
 *  drive[2]:   selected drive 0..3
 *  reset_not:  0 = hold controller in reset, 1 = run
 *  dma_irq:    1 = enable DMA/IRQ pin (required for any normal use)
 *  motor_*:    1 = spin drive N's motor
 */
struct fdc_dor {
    u8 drive : 2;
    u8 reset_not : 1;
    u8 dma_irq : 1;
    u8 motor_0 : 1;
    u8 motor_1 : 1;
    u8 motor_2 : 1;
    u8 motor_3 : 1;
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
    u8 stop : 1;
    u8 start : 1;
    u8 transmit : 1;
    u8 rd : 3;
    u8 page : 2;
};

/* Data configuration register (offset 0x0E, page 0). */
struct ne2k_dcr {
    u8 wts : 1; /* word transfer select (1 = 16-bit) */
    u8 bos : 1; /* byte order select (0 = little-endian) */
    u8 las : 1; /* long address select (0 = 16-bit DMA) */
    u8 ls : 1;  /* loopback select (0 = normal) */
    u8 arm : 1; /* auto-init remote (0 = disabled) */
    u8 ft : 2;  /* FIFO threshold */
    u8 : 1;
};

/* NE2000 interrupt mask register (offset 0x0F, page 0).
 *
 *  Bit layout mirrors ISR: 1 = enabled, 0 = masked.  Bit 7 is reserved.
 */
struct ne2k_imr {
    u8 prx : 1;
    u8 ptx : 1;
    u8 rxe : 1;
    u8 txe : 1;
    u8 ovw : 1;
    u8 cnt : 1;
    u8 rdc : 1;
    u8 : 1;
};

/* NE2000 interrupt status register (offset 0x07, page 0).
 *
 *  Writing a 1 to a bit acks it.  ``rst`` is read-only.
 */
struct ne2k_isr {
    u8 prx : 1; /* packet received OK */
    u8 ptx : 1; /* packet transmitted OK */
    u8 rxe : 1; /* receive error */
    u8 txe : 1; /* transmit error */
    u8 ovw : 1; /* RX-ring overwrite warning */
    u8 cnt : 1; /* counter overflow */
    u8 rdc : 1; /* remote DMA complete */
    u8 rst : 1; /* reset status */
};

/* Receive configuration register (offset 0x0C, page 0). */
struct ne2k_rcr {
    u8 sep : 1; /* save errored packets */
    u8 ar : 1;  /* accept runt packets */
    u8 ab : 1;  /* accept broadcast */
    u8 am : 1;  /* accept multicast */
    u8 pro : 1; /* promiscuous physical */
    u8 mon : 1; /* monitor mode (no RX) */
    u8 : 2;
};

/* Transmit configuration register (offset 0x0D, page 0). */
struct ne2k_tcr {
    u8 crc : 1;  /* inhibit CRC */
    u8 lb : 2;   /* loopback control */
    u8 atd : 1;  /* auto-transmit disable */
    u8 ofst : 1; /* collision-offset enable */
    u8 : 3;
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
    u8 irq0 : 1;
    u8 irq1 : 1;
    u8 irq2 : 1;
    u8 irq3 : 1;
    u8 irq4 : 1;
    u8 irq5 : 1;
    u8 irq6 : 1;
    u8 irq7 : 1;
};

#endif
