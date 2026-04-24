;;; ------------------------------------------------------------------------
;;; ata.asm — native ATA PIO driver (primary controller, master drive).
;;;
;;; Replaces the INT 13h-based sector I/O that fs/block.asm used to do.  Talks
;;; to the primary IDE controller at 0x1F0..0x1F7 in LBA28 PIO mode.  The
;;; caller-facing surface is unchanged:
;;;     read_sector   AX = 0-based LBA; fills SECTOR_BUFFER; CF on error.
;;;     write_sector  AX = 0-based LBA; writes SECTOR_BUFFER; CF on error.
;;; One sector at a time — matches the existing filesystem layer.
;;;
;;; Stage 1 MBR still uses INT 13h to load stage 2.  That's intentional:
;;; stage 1 stays BIOS-dependent and 16-bit real mode.  Only the post-boot
;;; disk I/O flows through this driver.
;;; ------------------------------------------------------------------------

        ATA_CMD_READ            equ 20h
        ATA_CMD_WRITE           equ 30h
        ATA_COMMAND             equ 1F7h
        ATA_DATA                equ 1F0h
        ATA_DEV_CTRL            equ 3F6h        ; Device Control register
        ATA_DEV_CTRL_SRST       equ 04h         ; software reset bit
        ATA_DRIVE               equ 1F6h
        ATA_DRIVE_MASTER_LBA    equ 0E0h
        ATA_LBA0                equ 1F3h
        ATA_LBA1                equ 1F4h
        ATA_LBA2                equ 1F5h
        ATA_SEC_COUNT           equ 1F2h
        ATA_STATUS              equ 1F7h
        ATA_STATUS_BSY          equ 80h
        ATA_STATUS_DRQ          equ 08h
        ATA_STATUS_ERR          equ 01h

ata_init:
        ;; Software-reset the primary ATA controller and wait for BSY to
        ;; clear.  Four back-to-back reads of the Device Control register
        ;; provide the 400 ns hold time the spec requires before releasing
        ;; SRST.  Call once at boot before the first read or write.
        push eax
        push edx
        mov dx, ATA_DEV_CTRL
        mov al, ATA_DEV_CTRL_SRST
        out dx, al
        in al, dx               ; 400 ns delay
        in al, dx
        in al, dx
        in al, dx
        xor al, al
        out dx, al
        mov dx, ATA_STATUS
        .wait:
        in al, dx
        test al, ATA_STATUS_BSY
        jnz .wait
        pop edx
        pop eax
        ret

ata_issue:
        ;; Input: AX = 0-based LBA (low 16 bits; LBA28 high bits = 0),
        ;;        BL = command byte (ATA_CMD_READ or ATA_CMD_WRITE).
        ;; Waits for BSY clear, programs drive/LBA/count/command.
        ;; Preserves all registers.
        push eax
        push ebx
        push ecx
        push edx

        mov cx, ax                      ; stash LBA across the port writes

        mov dx, ATA_STATUS
        .wait_busy:
        in al, dx
        test al, ATA_STATUS_BSY
        jnz .wait_busy

        mov dx, ATA_DRIVE
        mov al, ATA_DRIVE_MASTER_LBA
        out dx, al

        mov dx, ATA_SEC_COUNT
        mov al, 1
        out dx, al

        mov dx, ATA_LBA0
        mov al, cl
        out dx, al
        mov dx, ATA_LBA1
        mov al, ch
        out dx, al
        mov dx, ATA_LBA2
        xor al, al
        out dx, al

        mov dx, ATA_COMMAND
        mov al, bl
        out dx, al

        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

ata_read_sector:
        ;; Input:  AX = 0-based logical sector number.
        ;; Output: SECTOR_BUFFER filled with 512 bytes.  CF=1 on error.
        push eax
        push ebx
        push ecx
        push edx
        push edi

        mov bl, ATA_CMD_READ
        call ata_issue
        call ata_wait_drq
        jc .done

        mov dx, ATA_DATA
        mov edi, SECTOR_BUFFER
        mov ecx, 256
        cld
        rep insw
        clc

        .done:
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

ata_wait_drq:
        ;; Spin until BSY clear, then return CF=1 if ERR, CF=0 if DRQ.
        ;; Clobbers AX.  Preserves everything else.
        push edx
        mov dx, ATA_STATUS
        .poll:
        in al, dx
        test al, ATA_STATUS_BSY
        jnz .poll
        test al, ATA_STATUS_ERR
        jnz .err
        test al, ATA_STATUS_DRQ
        jz .poll
        clc
        pop edx
        ret
        .err:
        stc
        pop edx
        ret

ata_write_sector:
        ;; Input:  AX = 0-based logical sector number; SECTOR_BUFFER holds
        ;;         the 512 bytes to write.
        ;; Output: CF=1 on error.
        push eax
        push ebx
        push ecx
        push edx
        push esi

        mov bl, ATA_CMD_WRITE
        call ata_issue
        call ata_wait_drq
        jc .done

        mov dx, ATA_DATA
        mov esi, SECTOR_BUFFER
        mov ecx, 256
        cld
        rep outsw

        mov dx, ATA_STATUS
        .wait_done:
        in al, dx
        test al, ATA_STATUS_BSY
        jnz .wait_done
        test al, ATA_STATUS_ERR
        jnz .err
        clc
        jmp .done
        .err:
        stc

        .done:
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
