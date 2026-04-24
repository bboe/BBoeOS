;;; ------------------------------------------------------------------------
;;; pic.asm — 8259A master/slave initialization and vector remap.
;;;
;;; Remaps the legacy BIOS vectors (master IRQ 0-7 at 0x08-0x0F, slave
;;; IRQ 8-15 at 0x70-0x77) to 0x20-0x27 / 0x28-0x2F.  Required before the
;;; pmode flip: CPU exceptions 0-31 occupy 0x08-0x1F, so the default BIOS
;;; layout aliases IRQ 0 onto the double-fault vector and IRQ 5 onto #GP.
;;; Also required by the IDT in arch/idt.asm, which reserves slots 32..47
;;; for remapped IRQs and leaves them not-present until drivers fill them.
;;;
;;; Leaves all IRQs masked.  Each driver (rtc_tick_init, fdc_install_irq,
;;; …) unmasks its own line after installing a handler.  ps2_init's
;;; explicit IRQ 1 mask is now redundant but harmless.
;;;
;;; Enters and exits with IF clear — caller re-enables interrupts once
;;; handlers for the lines it intends to unmask are in place.
;;; ------------------------------------------------------------------------

        PIC1_CMD_PORT   equ 20h
        PIC1_DATA_PORT  equ 21h
        PIC2_CMD_PORT   equ 0A0h
        PIC2_DATA_PORT  equ 0A1h

        ICW1_INIT       equ 11h         ; begin init, cascaded, expect ICW4
        ICW4_8086       equ 01h         ; 8086/88 mode, normal EOI
        PIC1_VECTOR     equ 20h         ; master IRQ 0..7 → 0x20..0x27
        PIC2_VECTOR     equ 28h         ; slave  IRQ 8..15 → 0x28..0x2F
        PIC1_CASCADE    equ 04h         ; master: slave present on IRQ 2
        PIC2_CASCADE_ID equ 02h         ; slave: cascade identity = 2
        PIC_MASK_ALL    equ 0FFh

pic_remap:
        cli

        ;; ICW1 — start init sequence on both PICs.
        mov al, ICW1_INIT
        out PIC1_CMD_PORT, al
        out PIC2_CMD_PORT, al

        ;; ICW2 — vector offsets.
        mov al, PIC1_VECTOR
        out PIC1_DATA_PORT, al
        mov al, PIC2_VECTOR
        out PIC2_DATA_PORT, al

        ;; ICW3 — cascade wiring.
        mov al, PIC1_CASCADE
        out PIC1_DATA_PORT, al
        mov al, PIC2_CASCADE_ID
        out PIC2_DATA_PORT, al

        ;; ICW4 — 8086 mode.
        mov al, ICW4_8086
        out PIC1_DATA_PORT, al
        out PIC2_DATA_PORT, al

        ;; Mask every line; drivers unmask the IRQs they own.
        mov al, PIC_MASK_ALL
        out PIC1_DATA_PORT, al
        out PIC2_DATA_PORT, al

        ret
