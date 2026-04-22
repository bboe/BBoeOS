;;; Dispatch sector I/O to the driver that matches the boot device:
;;; BIOS passes DL=0x00 for floppy A:, 0x80+ for IDE HDDs.  Stage 1
;;; stashed that in boot_disk.  Stage 2's filesystem layer calls
;;; read_sector / write_sector blind to the medium.
read_sector:
        cmp byte [boot_disk], 80h
        jb fdc_read_sector
        jmp ata_read_sector

write_sector:
        cmp byte [boot_disk], 80h
        jb fdc_write_sector
        jmp ata_write_sector
