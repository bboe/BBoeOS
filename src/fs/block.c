// block.c — block I/O dispatcher.  Routes read_sector / write_sector to the
// fdc or ata driver based on the boot device BIOS handed us in DL (0x00 for
// floppy A:, 0x80+ for IDE HDDs; saved in boot_disk by stage 1).  The
// filesystem layer above calls these blind to the medium.
//
// Each wrapper is __attribute__((naked)) so cc.py emits no prologue or
// epilogue, pins the in_register("ax") parameter directly to AX, and
// resolves the if/else into two tail jmps.  uint8_t boot_disk paired
// with `< 0x80` triggers the unsigned compare path (jb / jae), so the
// peephole optimizer collapses the false-branch jump and the unconditional
// tail jmp into the asm-version's exact 3-instruction shape:
//
//     cmp byte [boot_disk], 128
//     jb fdc_read_sector
//     jmp ata_read_sector

uint8_t boot_disk __attribute__((asm_name("boot_disk")));

__attribute__((carry_return)) int ata_read_sector(int sector __attribute__((in_register("ax"))));
__attribute__((carry_return)) int ata_write_sector(int sector __attribute__((in_register("ax"))));
__attribute__((carry_return)) int fdc_read_sector(int sector __attribute__((in_register("ax"))));
__attribute__((carry_return)) int fdc_write_sector(int sector __attribute__((in_register("ax"))));

__attribute__((carry_return)) __attribute__((naked))
int read_sector(int sector __attribute__((in_register("ax")))) {
    if (boot_disk < 0x80) {
        fdc_read_sector(sector);
    } else {
        ata_read_sector(sector);
    }
}

__attribute__((carry_return)) __attribute__((naked))
int write_sector(int sector __attribute__((in_register("ax")))) {
    if (boot_disk < 0x80) {
        fdc_write_sector(sector);
    } else {
        ata_write_sector(sector);
    }
}
