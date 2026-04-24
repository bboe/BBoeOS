;;; ------------------------------------------------------------------------
;;; bboeos.asm — top-level flat-binary image.
;;;
;;; Assembled with `nasm -f bin`, loaded at org 7C00h.  Layout:
;;;   [0x7C00] 512-byte MBR — full pre-flip boot sequence
;;;   [0x7E00] GDT + kernel binary — 32-bit flat descriptor tables then code
;;;
;;; MBR execution order:
;;;   start: setup → disk read → pic_remap → lidt → A20 → lgdt → CR0.PE flip
;;;   → far-jmp to `protected_mode_entry` in entry.asm.
;;;
;;; On disk error we print a single '!' via INT 10h AH=0Eh and halt.
;;;
;;; GDT layout:
;;;   0x00 null
;;;   0x08 code: base=0, limit=4GB, 32-bit, DPL=0, exec/read
;;;   0x10 data: base=0, limit=4GB, 32-bit, DPL=0, read/write
;;; ------------------------------------------------------------------------

        org 7C00h               ; offset where bios loads our first stage
        %include "constants.asm"

        ICW1_INIT       equ 11h         ; begin init, cascaded, expect ICW4
        ICW4_8086       equ 01h         ; 8086/88 mode, normal EOI
        PIC1_CASCADE    equ 04h         ; master: slave present on IRQ 2
        PIC1_CMD_PORT   equ 20h
        PIC1_DATA_PORT  equ 21h
        PIC1_VECTOR     equ 20h         ; master IRQ 0..7 → 0x20..0x27
        PIC2_CASCADE_ID equ 02h         ; slave: cascade identity = 2
        PIC2_CMD_PORT   equ 0A0h
        PIC2_DATA_PORT  equ 0A1h
        PIC2_VECTOR     equ 28h         ; slave  IRQ 8..15 → 0x28..0x2F
        PIC_MASK_ALL    equ 0FFh

start:
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov [boot_disk], dl

        ;; Dedicated stack at SS=0x9000, SP=0xFFF0 (linear 0x90000-0x9FFF0)
        ;; owns its entire segment and can never collide with the
        ;; kernel, disk buffers, or loaded programs in segment 0.
        cli
        mov ax, 9000h
        mov ss, ax
        mov sp, 0FFF0h
        sti

        ;; Reset disk controllers before the first read; defensive on
        ;; real hardware, no-op on QEMU.
        xor ax, ax
        int 13h
        jc .error

        ;; Read stage2 at CHS (cyl=0, head=0, sector=2) into linear 0x7E00.
        ;; The byte count lives in `stage2_bytes` (NASM-computed from
        ;; kernel_end - 7E00h and placed at MBR offset 508), so host tools
        ;; can read the same value from the drive image.  Here we shift right
        ;; by 9 to get the sector count, and publish `directory_sector` =
        ;; stage2_sectors + 1 for bbfs / ext2 to consume.
        mov ax, [stage2_bytes]
        add ax, 511
        shr ax, 9
        mov [directory_sector], ax
        inc word [directory_sector]
        mov ah, 02h             ; BIOS read-sectors function (AL = count)
        mov bx, 7E00h
        mov cx, 2
        mov dh, 0
        mov dl, [boot_disk]
        int 13h
        jc .error

        ;; Remap 8259A master/slave vectors to 0x20..0x27 / 0x28..0x2F.
        ;; Required before the pmode flip: CPU exceptions 0-31 occupy
        ;; 0x08-0x1F, aliasing IRQ 0 onto double-fault and IRQ 5 onto #GP
        ;; under the BIOS default layout.  Leaves all IRQ lines masked.

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

        ;; Mask every line; drivers unmask the IRQs they own post-flip.
        mov al, PIC_MASK_ALL
        out PIC1_DATA_PORT, al
        out PIC2_DATA_PORT, al

        lidt [idtr]

        ;; Fast-A20 via port 0x92, bit 1. Bit 0 triggers a warm reset, so mask
        ;; it off before writing. On QEMU this is reliably available; on real
        ;; hardware the keyboard-controller path is the fallback but we don't
        ;; need it for the targets we run on.
        in al, 0x92
        test al, 0x02
        jnz .a20_ready
        or al, 0x02
        and al, 0xFE
        out 0x92, al
        .a20_ready:

        lgdt [pmode_gdtr]

        mov eax, cr0
        or eax, 1
        mov cr0, eax

        ;; Far jump with 32-bit offset flushes the prefetch queue and loads
        ;; CS with the 32-bit code selector.
        jmp dword 0x08:protected_mode_entry

        .error:
        mov ax, 0E00h | '!'
        xor bx, bx
        int 10h
        .halt:
        hlt
        jmp .halt

boot_disk db 0
directory_sector dw 0           ; stage2_sectors + 1; set at boot, read by bbfs

        times 508-($-$$) db 0
stage2_bytes dw kernel_end - 7E00h      ; fixed offset 508; host tools depend on it
        dw 0AA55h

        ;; GDT descriptors. Encoded by hand rather than via `dq` math so the
        ;; field meanings stay visible to a reader.
        align 8
pmode_gdt_start:
        dq 0                            ; 0x00 null

        ;; 0x08 code segment (CS): base=0, limit=0xFFFFF (× 4KB = 4GB).
        ;; Access byte 10011010b  = P=1 DPL=00 S=1 type=1010 (exec/read, non-conforming)
        ;; Flags     11001111b    = G=1 D=1 L=0 AVL=0, limit[19:16]=0xF
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10011010b
        db 11001111b
        db 0x00

        ;; 0x10 data segment (DS/ES/SS/FS/GS): same geometry, type=0010 (R/W).
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10010010b
        db 11001111b
        db 0x00
pmode_gdt_end:

pmode_gdtr:
        dw pmode_gdt_end - pmode_gdt_start - 1
        dd pmode_gdt_start

[bits 32]
%include "drivers/ata.asm"              ; ATA PIO disk driver
%include "drivers/console.asm"          ; serial_character (COM1 output)
%include "drivers/fdc.asm"              ; floppy DMA + IRQ 6 driver
%include "drivers/ps2.asm"              ; PS/2 keyboard driver (IRQ-driven)
%include "drivers/rtc.asm"              ; system_ticks / PIT constants
%include "drivers/vga.asm"              ; VGA text driver (32-bit flat addressing)
%include "fs/block.asm"                 ; read_sector / write_sector dispatch
%include "fs/vfs.asm"                   ; VFS dispatch + bbfs + ext2
%include "idt.asm"                      ; 32-bit IDT + exception stubs
%include "entry.asm"                    ; protected_mode_entry + post-flip init

kernel_end:
