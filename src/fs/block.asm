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
        ;; 0xC000F000 (`sector_buffer` EQU in kernel.asm).  bbfs.asm /
        ;; ext2.asm reach it via 32-bit `[ebx+offset]` etc. (PR A
        ;; migration), so the buffer no longer needs to fit in 16-bit
        ;; register addressing.  Reserved at boot by the bitmap
        ;; allocator's LOW_RESERVE_BYTES carve-out.
