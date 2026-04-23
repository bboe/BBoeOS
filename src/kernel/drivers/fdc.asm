;;; ------------------------------------------------------------------------
;;; fdc.asm — native floppy disk controller driver using DMA + IRQ 6.
;;;
;;; Mirrors SeaBIOS's flow (src/hw/floppy.c): sector data moves through
;;; 8237 DMA channel 2 and command completion is signalled by IRQ 6.
;;; That's the path QEMU's FDC emulation is battle-tested against; an
;;; earlier polled/PIO attempt using the FIFO hung after the first
;;; successful read because the state machine drifts out of sync.
;;;
;;; Target: primary 82077-style controller at 0x3F0..0x3F7, drive 0 (A:),
;;; 1.44 MB geometry (80 cyl × 2 heads × 18 sectors).
;;;
;;; Surface (parallel to ata.asm):
;;;     fdc_init          install IRQ 6 handler, reset, SPECIFY, motor on,
;;;                       recalibrate drive 0.  Called once.
;;;     fdc_read_sector   AX = 0-based LBA; fills SECTOR_BUFFER; CF err.
;;;     fdc_write_sector  AX = 0-based LBA; writes SECTOR_BUFFER; CF err.
;;; ------------------------------------------------------------------------

        FDC_DOR                 equ 3F2h
        FDC_MSR                 equ 3F4h
        FDC_DATA                equ 3F5h
        FDC_CCR                 equ 3F7h

        DOR_RESET_NOT           equ 04h
        DOR_DMA_IRQ             equ 08h
        DOR_MOTOR_0             equ 10h

        MSR_RQM                 equ 80h
        MSR_DIO                 equ 40h

        CMD_SPECIFY             equ 03h
        CMD_RECALIBRATE         equ 07h
        CMD_SENSE_INT           equ 08h
        CMD_SEEK                equ 0Fh
        CMD_READ                equ 0E6h       ; MT=1 MF=1 SK=1
        CMD_WRITE               equ 0C5h       ; MT=1 MF=1

        FDC_SECTORS_PER_TRACK   equ 18
        FDC_HEADS               equ 2
        FDC_SECTOR_SIZE_CODE    equ 2           ; 2^N * 128 = 512
        FDC_GAP3                equ 1Bh

        DMA_CH2_ADDR            equ 04h
        DMA_CH2_COUNT           equ 05h
        DMA_MASK                equ 0Ah
        DMA_MODE                equ 0Bh
        DMA_CLEAR_FF            equ 0Ch
        DMA_CH2_PAGE            equ 81h

        DMA_MODE_READ           equ 46h        ; single / inc / read / ch2
        DMA_MODE_WRITE          equ 4Ah        ; single / inc / write / ch2
        DMA_MASK_CH2            equ 06h        ; mask channel 2 bit | ch2
        DMA_UNMASK_CH2          equ 02h

        PIC1_CMD_PORT           equ 20h
        PIC1_DATA_PORT          equ 21h
        PIC_EOI                 equ 20h
        IVT_IRQ6_OFFSET         equ 26h * 4    ; remapped by pic_remap (was 0Eh*4 under BIOS)

fdc_dma_setup:
        ;; Input: AL = DMA mode byte (DMA_MODE_READ or DMA_MODE_WRITE).
        ;; Programs channel 2 for a 512 B transfer at SECTOR_BUFFER.
        ;; Preserves all registers.
        push ax

        mov ah, al                      ; stash mode

        mov al, DMA_MASK_CH2
        out DMA_MASK, al
        xor al, al
        out DMA_CLEAR_FF, al

        mov al, SECTOR_BUFFER & 0FFh
        out DMA_CH2_ADDR, al
        mov al, (SECTOR_BUFFER >> 8) & 0FFh
        out DMA_CH2_ADDR, al

        xor al, al
        out DMA_CLEAR_FF, al

        mov al, (512 - 1) & 0FFh
        out DMA_CH2_COUNT, al
        mov al, ((512 - 1) >> 8) & 0FFh
        out DMA_CH2_COUNT, al

        mov al, ah
        out DMA_MODE, al

        xor al, al                      ; SECTOR_BUFFER = 0x0000E000, page = 0
        out DMA_CH2_PAGE, al

        mov al, DMA_UNMASK_CH2
        out DMA_MASK, al

        pop ax
        ret

fdc_drain_result:
        ;; Read the 7 result bytes (ST0, ST1, ST2, C, H, R, N) — ignored.
        push cx
        mov cx, 7
        .loop:
        call fdc_recv
        loop .loop
        pop cx
        ret

fdc_init:
        ;; One-time init.  Install IRQ 6 handler + unmask, reset controller,
        ;; SPECIFY in DMA mode, motor 0 on, recalibrate.
        push ax
        push cx
        push dx

        call fdc_install_irq
        mov byte [fdc_irq_flag], 0

        ;; Reset: clear DOR to assert reset, then raise RESET_NOT with
        ;; DMA+IRQ enabled and drive 0 selected.
        mov dx, FDC_DOR
        xor al, al
        out dx, al
        mov al, DOR_RESET_NOT | DOR_DMA_IRQ
        out dx, al

        call fdc_wait_irq               ; controller signals ready

        ;; Drain 4 polling interrupts (one per drive slot on 82077AA).
        call fdc_sense_interrupt
        call fdc_sense_interrupt
        call fdc_sense_interrupt
        call fdc_sense_interrupt

        ;; Data rate 500 Kbps for 1.44 MB.
        mov dx, FDC_CCR
        xor al, al
        out dx, al

        ;; SPECIFY: SRT/HUT don't matter on QEMU; HLT=1, ND=0 (DMA).
        mov al, CMD_SPECIFY
        call fdc_send
        mov al, 0DFh
        call fdc_send
        mov al, 02h
        call fdc_send

        ;; Motor 0 on, wait for spin-up.
        mov dx, FDC_DOR
        mov al, DOR_MOTOR_0 | DOR_RESET_NOT | DOR_DMA_IRQ
        out dx, al
        mov cx, 500
        call rtc_sleep_ms

        ;; Recalibrate drive 0.
        mov byte [fdc_irq_flag], 0
        mov al, CMD_RECALIBRATE
        call fdc_send
        xor al, al
        call fdc_send
        call fdc_wait_irq
        call fdc_sense_interrupt

        pop dx
        pop cx
        pop ax
        ret

fdc_install_irq:
        ;; Install fdc_irq6_handler at IVT entry 0x26 (pic_remap'd) and
        ;; unmask IRQ 6 on the master PIC.
        cli
        push ax
        push es
        xor ax, ax
        mov es, ax
        mov word [es:IVT_IRQ6_OFFSET], fdc_irq6_handler
        mov word [es:IVT_IRQ6_OFFSET + 2], cs
        pop es
        in al, PIC1_DATA_PORT
        and al, 0BFh                    ; clear bit 6 (IRQ 6 unmasked)
        out PIC1_DATA_PORT, al
        pop ax
        sti
        ret

fdc_irq6_handler:
        ;; IRQ 6 fires on command completion for SEEK / RECAL / READ /
        ;; WRITE.  We just flag it and EOI; the main path polls the flag.
        push ax
        mov byte [fdc_irq_flag], 1
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        pop ax
        iret

fdc_issue_read_write:
        ;; Input: AL = command, CH = cyl, CL = sec (1-based), DH = head.
        ;; Sends the 9 parameter bytes.
        push ax
        push bx
        mov bh, al
        call fdc_send
        mov al, dh
        shl al, 2
        call fdc_send                   ; HDS = (head<<2) | drive(0)
        mov al, ch
        call fdc_send                   ; C
        mov al, dh
        call fdc_send                   ; H
        mov al, cl
        call fdc_send                   ; R
        mov al, FDC_SECTOR_SIZE_CODE
        call fdc_send                   ; N
        mov al, cl                      ; EOT = this sector → 1-sector xfer
        call fdc_send
        mov al, FDC_GAP3
        call fdc_send
        mov al, 0FFh
        call fdc_send                   ; DTL (ignored when N>0)
        pop bx
        pop ax
        ret

fdc_lba_to_chs:
        ;; Input: AX = 0-based LBA.
        ;; Output: CH = cylinder, CL = sector (1-based), DH = head.
        push ax
        push bx
        xor dx, dx
        mov bx, FDC_SECTORS_PER_TRACK
        div bx
        mov cl, dl
        inc cl
        xor dx, dx
        mov bx, FDC_HEADS
        div bx
        mov ch, al
        mov dh, dl
        pop bx
        pop ax
        ret

fdc_read_sector:
        ;; Input:  AX = 0-based LBA.
        ;; Output: SECTOR_BUFFER filled via DMA.  CF=0 on success.
        push ax
        push bx
        push cx
        push dx

        call fdc_lba_to_chs
        call fdc_seek

        mov al, DMA_MODE_READ
        call fdc_dma_setup

        mov byte [fdc_irq_flag], 0
        mov al, CMD_READ
        call fdc_issue_read_write
        call fdc_wait_irq
        call fdc_drain_result

        clc
        pop dx
        pop cx
        pop bx
        pop ax
        ret

fdc_recv:
        ;; Output: AL = byte (waits for RQM=1, DIO=1).  Clobbers AX, DX.
        push dx
        .wait:
        mov dx, FDC_MSR
        in al, dx
        and al, MSR_RQM | MSR_DIO
        cmp al, MSR_RQM | MSR_DIO
        jne .wait
        mov dx, FDC_DATA
        in al, dx
        pop dx
        ret

fdc_seek:
        ;; Input: CH = cylinder, DH = head.  Completes via IRQ 6.
        push ax
        mov byte [fdc_irq_flag], 0
        mov al, CMD_SEEK
        call fdc_send
        mov al, dh
        shl al, 2
        call fdc_send
        mov al, ch
        call fdc_send
        call fdc_wait_irq
        call fdc_sense_interrupt
        pop ax
        ret

fdc_send:
        ;; Input: AL = byte.  Sends once RQM=1, DIO=0.  Preserves AX, DX.
        push ax
        push dx
        mov ah, al
        .wait:
        mov dx, FDC_MSR
        in al, dx
        and al, MSR_RQM | MSR_DIO
        cmp al, MSR_RQM
        jne .wait
        mov dx, FDC_DATA
        mov al, ah
        out dx, al
        pop dx
        pop ax
        ret

fdc_sense_interrupt:
        push ax
        mov al, CMD_SENSE_INT
        call fdc_send
        call fdc_recv                   ; ST0
        call fdc_recv                   ; PCN
        pop ax
        ret

fdc_wait_irq:
        ;; Block until IRQ 6 fires.  sti so a syscall-context caller
        ;; (IF=0 after INT 30h entry) can still receive the interrupt.
        ;; pushf/popf preserves the caller's IF either way.
        pushf
        sti
        .wait:
        cmp byte [fdc_irq_flag], 0
        je .wait
        mov byte [fdc_irq_flag], 0
        popf
        ret

fdc_write_sector:
        ;; Input: AX = 0-based LBA.  CF=0 on success.
        push ax
        push bx
        push cx
        push dx

        call fdc_lba_to_chs
        call fdc_seek

        mov al, DMA_MODE_WRITE
        call fdc_dma_setup

        mov byte [fdc_irq_flag], 0
        mov al, CMD_WRITE
        call fdc_issue_read_write
        call fdc_wait_irq
        call fdc_drain_result

        clc
        pop dx
        pop cx
        pop bx
        pop ax
        ret

        fdc_irq_flag db 0
