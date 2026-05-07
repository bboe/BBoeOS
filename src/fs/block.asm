;;; block.asm — block I/O dispatcher.  Routes disk_read_sector /
;;; disk_write_sector to the fdc or ata driver based on the boot
;;; device BIOS handed us in DL (0x00 for floppy A:, 0x80+ for IDE
;;; HDDs; saved in boot_disk by stage 1).  fs/sector_cache.c sits
;;; in front of these as the cache-aware entry points
;;; (read_sector / write_sector) the FS layer actually calls; the
;;; cache C falls through to disk_*_sector on a miss.
disk_read_sector:
        cmp byte [boot_disk], 80h
        jb fdc_read_sector
        jmp ata_read_sector

;;; disk_read_sectors: bulk read entry point.  AX = start LBA,
;;; CX = sector count (1..256), EDI = destination kernel-virt.
;;; Same dispatch shape as disk_read_sector — just routes the
;;; multi-sector helper instead.  The cache layer calls this on a
;;; miss to prefetch a run of sectors in one driver round-trip.
disk_read_sectors:
        cmp byte [boot_disk], 80h
        jb fdc_read_sectors
        jmp ata_read_sectors

disk_write_sector:
        cmp byte [boot_disk], 80h
        jb fdc_write_sector
        jmp ata_write_sector
