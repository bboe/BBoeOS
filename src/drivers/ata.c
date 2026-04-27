// ata.c — native ATA PIO driver (primary controller, master drive).
//
// Replaces the INT 13h-based sector I/O that fs/block.asm used to do.  Talks
// to the primary IDE controller at 0x1F0..0x1F7 in LBA28 PIO mode.  The
// caller-facing surface is unchanged:
//     ata_read_sector   AX = 0-based LBA; fills SECTOR_BUFFER; CF on error.
//     ata_write_sector  AX = 0-based LBA; writes SECTOR_BUFFER; CF on error.
// One sector at a time — matches the existing filesystem layer.
//
// Stage 1 MBR still uses INT 13h to load stage 2.  That's intentional:
// stage 1 stays BIOS-dependent and 16-bit real mode.  Only the post-boot
// disk I/O flows through this driver.
//
// preserve_register attributes mirror the asm version's push / pop fan
// around the body so callers in fs/bbfs.asm and fs/ext2.asm continue to
// see BX / CX / DX / DI / SI intact across the call.  AX is the
// in_register input — its post-call value isn't part of the contract
// (every caller reloads it before reuse).

#define ATA_DATA      0x1F0
#define ATA_SEC_COUNT 0x1F2
#define ATA_LBA0      0x1F3
#define ATA_LBA1      0x1F4
#define ATA_LBA2      0x1F5
#define ATA_DRIVE     0x1F6
#define ATA_STATUS    0x1F7
#define ATA_COMMAND   0x1F7

#define ATA_STATUS_ERR 0x01
#define ATA_STATUS_DRQ 0x08
#define ATA_STATUS_BSY 0x80

#define ATA_CMD_READ          0x20
#define ATA_CMD_WRITE         0x30
#define ATA_DRIVE_MASTER_LBA  0xE0

// ata_issue: program drive / LBA / count / command for a one-sector op.
// Internal helper.  Spins until BSY clears, then writes the seven port
// registers in order.
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
void ata_issue(int lba __attribute__((in_register("ax"))),
               int command __attribute__((in_register("bx")))) {
    while ((inb(ATA_STATUS) & ATA_STATUS_BSY) != 0) {
    }
    outb(ATA_DRIVE, ATA_DRIVE_MASTER_LBA);
    outb(ATA_SEC_COUNT, 1);
    outb(ATA_LBA0, lba & 0xFF);
    outb(ATA_LBA1, (lba >> 8) & 0xFF);
    outb(ATA_LBA2, 0);
    outb(ATA_COMMAND, command);
}

// ata_wait_drq: spin until BSY clears, then return success (DRQ set) /
// failure (ERR set).  Internal helper used by both read and write paths.
__attribute__((carry_return)) __attribute__((preserve_register("dx")))
int ata_wait_drq() {
    int status;
    while (1) {
        status = inb(ATA_STATUS);
        if ((status & ATA_STATUS_BSY) != 0) {
            continue;
        }
        if ((status & ATA_STATUS_ERR) != 0) {
            return 0;
        }
        if ((status & ATA_STATUS_DRQ) != 0) {
            return 1;
        }
    }
}

// ata_read_sector: read one 512-byte sector (LBA28) into SECTOR_BUFFER.
// rep insw stays as inline asm — cc.py has no insw / outsw builtins.
__attribute__((carry_return))
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx"))) __attribute__((preserve_register("di")))
int ata_read_sector(int lba __attribute__((in_register("ax")))) {
    ata_issue(lba, ATA_CMD_READ);
    if (!ata_wait_drq()) {
        return 0;
    }
    asm("mov dx, 0x1F0\n"
        "mov di, SECTOR_BUFFER\n"
        "mov cx, 256\n"
        "cld\n"
        "rep insw");
    return 1;
}

// ata_write_sector: write 512 bytes from SECTOR_BUFFER to LBA.  After
// rep outsw, polls BSY/ERR one more time so a deferred-error completion
// surfaces as CF on return.
__attribute__((carry_return))
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx"))) __attribute__((preserve_register("si")))
int ata_write_sector(int lba __attribute__((in_register("ax")))) {
    int status;
    ata_issue(lba, ATA_CMD_WRITE);
    if (!ata_wait_drq()) {
        return 0;
    }
    asm("mov dx, 0x1F0\n"
        "mov si, SECTOR_BUFFER\n"
        "mov cx, 256\n"
        "cld\n"
        "rep outsw");
    while ((inb(ATA_STATUS) & ATA_STATUS_BSY) != 0) {
    }
    status = inb(ATA_STATUS);
    if ((status & ATA_STATUS_ERR) != 0) {
        return 0;
    }
    return 1;
}
