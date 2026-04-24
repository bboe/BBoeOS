;;; block.asm — block I/O dispatcher.  Routes read_sector / write_sector
;;; to the fdc or ata driver based on the boot device BIOS handed us in
;;; DL (0x00 for floppy A:, 0x80+ for IDE HDDs; saved in boot_disk by
;;; stage 1).  The filesystem layer above calls these blind to the
;;; medium.
read_sector:
        cmp byte [boot_disk], 80h
        jb fdc_read_sector
        jmp ata_read_sector

write_sector:
        cmp byte [boot_disk], 80h
        jb fdc_write_sector
        jmp ata_write_sector
