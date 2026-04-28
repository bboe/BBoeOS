// ata.c — native ATA PIO driver (primary controller, master drive).
//
// Replaces the INT 13h-based sector I/O for post-boot disk traffic.
// Talks to the primary IDE controller at 0x1F0..0x1F7 in LBA28 PIO mode.
// One sector at a time — matches the existing filesystem layer.
//
// Caller-facing surface, unchanged from the asm version:
//     ata_init                                 setup; call once at boot
//     ata_read_sector(AX=LBA)  → CF on error;  fills sector_buffer
//     ata_write_sector(AX=LBA) → CF on error;  writes sector_buffer
// `ata_issue` and `ata_wait_drq` are internal helpers.
//
// Stage 1 still uses INT 13h to load stage 2; only the post-boot disk
// I/O flows through this driver.

// Port addresses and command/status bits inlined as bare integers to
// avoid clashing with the shared asm %include namespace.
//   ATA_DATA              = 0x1F0   data port (16-bit)
//   ATA_SEC_COUNT         = 0x1F2   sector count
//   ATA_LBA0/1/2          = 0x1F3/4/5
//   ATA_DRIVE             = 0x1F6   drive/head select; master+LBA = 0xE0
//   ATA_COMMAND/STATUS    = 0x1F7
//   ATA_DEV_CTRL          = 0x3F6   software reset bit = 0x04
//   STATUS bits: BSY=0x80, DRQ=0x08, ERR=0x01
//   commands: READ = 0x20, WRITE = 0x30

void ata_init() {
    // Software-reset the primary controller, four 400ns reads on the
    // device-control register (see SRST hold-time spec), then release.
    kernel_outb(0x3F6, 0x04);
    kernel_inb(0x3F6);
    kernel_inb(0x3F6);
    kernel_inb(0x3F6);
    kernel_inb(0x3F6);
    kernel_outb(0x3F6, 0);
    while ((kernel_inb(0x1F7) & 0x80) != 0) {}
}

// Issue a one-sector ATA command.  Programs drive/LBA/count/command
// after waiting for BSY clear.  Caller polls DRQ via ata_wait_drq.
// AX = LBA (low 16 bits; LBA28 high = 0).  BL = command byte.
void ata_issue(int lba __attribute__((in_register("ax"))),
               uint8_t command __attribute__((in_register("bx"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
{
    int saved_lba;
    saved_lba = lba & 0xFFFF;
    while ((kernel_inb(0x1F7) & 0x80) != 0) {}
    kernel_outb(0x1F6, 0xE0);          // master + LBA mode
    kernel_outb(0x1F2, 1);             // sector count = 1
    kernel_outb(0x1F3, saved_lba & 0xFF);
    kernel_outb(0x1F4, (saved_lba >> 8) & 0xFF);
    kernel_outb(0x1F5, 0);
    kernel_outb(0x1F7, command);
}

// Forward decl: ata_read_sector and ata_write_sector come before
// ata_wait_drq alphabetically and need its signature.
int ata_wait_drq() __attribute__((carry_return))
    __attribute__((preserve_register("edx")));

// AX = LBA → sector_buffer filled.  Returns 1 on success / 0 on
// error (CF=0/CF=1 to asm callers respectively).  cc.py's
// ``carry_return`` convention prefers the positive-condition shape
// (``if (foo())`` over ``if (!foo())``) — the branch emission for
// the negated form is buggy at the time of writing.
int ata_read_sector(int lba __attribute__((in_register("ax"))))
    __attribute__((carry_return))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("edi")))
{
    ata_issue(lba, 0x20);                            // ATA_CMD_READ
    if (ata_wait_drq()) {
        kernel_insw(0x1F0, sector_buffer, 256);
        return 1;
    }
    return 0;
}

// Spin until BSY clears, then return CF=1 (= return 0) if ERR is
// set, CF=0 (= return 1) if DRQ is set.  cc.py's carry_return
// convention inverts the natural error reading: a `return 1` becomes
// CF clear, `return 0` becomes CF set.  So at the C level this
// function returns 1 on success and 0 on error, but the asm-side
// convention (CF=0 ok, CF=1 err) is preserved verbatim for callers
// reached via the asm calling shape.
int ata_wait_drq() __attribute__((carry_return))
    __attribute__((preserve_register("edx")))
{
    uint8_t status;
    while (1) {
        status = kernel_inb(0x1F7);
        if ((status & 0x80) != 0) { continue; }      // BSY — keep polling
        if ((status & 0x01) != 0) { return 0; }      // ERR → CF=1
        if ((status & 0x08) != 0) { return 1; }      // DRQ → CF=0
        // BSY=0, DRQ=0, ERR=0 — keep polling
    }
}

// AX = LBA, sector_buffer holds the bytes.  Same return shape as
// ata_read_sector.
int ata_write_sector(int lba __attribute__((in_register("ax"))))
    __attribute__((carry_return))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("esi")))
{
    uint8_t status;
    ata_issue(lba, 0x30);                            // ATA_CMD_WRITE
    if (ata_wait_drq()) {
        kernel_outsw(0x1F0, sector_buffer, 256);
        while (1) {
            status = kernel_inb(0x1F7);
            if ((status & 0x80) != 0) { continue; }
            if ((status & 0x01) != 0) { return 0; }
            return 1;
        }
    }
    return 0;
}
