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

        ;; The kernel disk buffer (`sector_buffer`) lives at fixed
        ;; low-physical 0xF000, accessed via the direct map at virt
        ;; 0xC000F000 (`sector_buffer` EQU in kernel.asm).  It stays
        ;; below the 16-bit pointer line so bbfs.asm / ext2.asm can
        ;; keep using `[bx+offset]` and `sub ax, sector_buffer` style
        ;; accesses without 32-bit-register conversion churn — the
        ;; high half (0xC000) is shared by the entire low-1 MB direct
        ;; map and gets folded out by NASM's truncation rules in 16-
        ;; bit operand contexts.  Reserved at boot by the bitmap
        ;; allocator's BOOT_REGION_BYTES carve-out (0x7C00..0xFFFF).
