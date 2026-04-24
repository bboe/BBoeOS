;;; ------------------------------------------------------------------------
;;; stage1_5.asm — 16-bit → 32-bit flat ring-0 protected-mode entry.
;;;
;;; The "stage 1.5" of the boot flow: stage 1 (MBR) loads stage 2 into
;;; memory, stage 2 runs real-mode init, then jumps here to switch CPU
;;; modes.  Once `enter_protected_mode` returns (via far jmp) the rest of
;;; stage 2 / the kernel runs in 32-bit flat pmode.
;;;
;;; Usage (from 16-bit real mode):
;;;     jmp enter_protected_mode
;;; Control transfers to `protected_mode_entry` running in 32-bit flat pmode
;;; with DS=ES=SS=FS=GS=0x10 and ESP=0x9FFF0. Never returns to the caller.
;;;
;;; The caller must define `protected_mode_entry` as a 32-bit label somewhere
;;; reachable with base=0 flat addressing (i.e., within the loaded image).
;;;
;;; GDT layout:
;;;   0x00 null
;;;   0x08 code: base=0, limit=4GB, 32-bit, DPL=0, exec/read
;;;   0x10 data: base=0, limit=4GB, 32-bit, DPL=0, read/write
;;; ------------------------------------------------------------------------

[bits 16]
enter_protected_mode:
        cli

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
        jmp dword 0x08:.pmode_entry

[bits 32]
        .pmode_entry:
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax

        ;; Stack sits in the top of conventional memory (linear 0x9FFF0).
        ;; With a flat data segment, ESP is the absolute linear address.
        mov esp, 0x9FFF0

        jmp protected_mode_entry

[bits 16]
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
