;;; ------------------------------------------------------------------------
;;; idt.asm — 32-bit IDT with exception stubs for protected-mode use.
;;;
;;; Exports a statically-built IDT covering vectors 0..31 (CPU exceptions)
;;; and 0x30 (INT 30h syscall gate).  Each exception stub normalizes the
;;; stack (pushes a fake error code when the CPU didn't), pushes the
;;; exception number, and jumps to a common handler that prints
;;; "EXCnn\r\n" to COM1 and halts.  No recovery — a panic is a panic.
;;;
;;; The module assembles 32-bit code (exception stubs + the INT 30h
;;; placeholder).  The IDT descriptor bytes are assembled as pure data so
;;; the [bits] mode doesn't affect them.
;;;
;;; Surface (called from real mode just before the pmode switch):
;;;     idt_install      - lidt [idtr].  Ownership of IDTR is ours.
;;;
;;; The pmode switch then jumps into 32-bit code, and any fault from that
;;; point vectors through our stubs.  PIC remap is orthogonal (belongs
;;; with the pmode switch itself); this module does not touch the PICs.
;;;
;;; Not yet wired into bboeos.asm.  Lives alongside boot/stage1_5.asm
;;; as standalone infrastructure until the final pmode integration.
;;; ------------------------------------------------------------------------

        IDT_CODE_SELECTOR       equ 08h          ; flat 32-bit code (pmode GDT[1])
        IDT_FLAGS_INT32         equ 8Eh          ; P=1 DPL=0 type=0xE
        COM1_DATA               equ 3F8h
        COM1_LSR                equ 3FDh
        LSR_THRE                equ 20h

%macro IDT_ENTRY 1
        ;; We rely on the kernel living entirely in the low 64 KB of
        ;; linear memory (stage 2 loads at 0x7E00 and never grows past
        ;; 0xFFFF), so offset 31:16 is always zero.  Revisit if anything
        ;; ever lives above that line.
        dw %1
        dw IDT_CODE_SELECTOR
        db 0
        db IDT_FLAGS_INT32
        dw 0
%endmacro

%macro EXC_NOERR 1
exc_%1:
        push dword 0                ; fake error code
        push dword %1
        jmp exc_common
%endmacro

%macro EXC_ERR 1
exc_%1:
        push dword %1               ; CPU already pushed the real error code
        jmp exc_common
%endmacro

[bits 32]

exc_common:
        ;; Stack on entry (top → bottom):
        ;;   [esp+0]  exception number (our push)
        ;;   [esp+4]  error code (real or faked 0)
        ;;   [esp+8]  EIP
        ;;   [esp+12] CS
        ;;   [esp+16] EFLAGS
        ;; We never return — just announce and halt.
        mov al, 'E'
        call exc_putc
        mov al, 'X'
        call exc_putc
        mov al, 'C'
        call exc_putc
        mov al, [esp]                   ; exception number
        shr al, 4
        call exc_puthex
        mov al, [esp]
        and al, 0Fh
        call exc_puthex
        mov al, 0Dh
        call exc_putc
        mov al, 0Ah
        call exc_putc
        cli
        .halt:
        hlt
        jmp .halt

exc_putc:
        ;; AL = char.  Writes to COM1 after polling LSR.THRE.  Preserves
        ;; EAX and EDX.
        push eax
        push edx
        mov ah, al
        .wait:
        mov dx, COM1_LSR
        in al, dx
        test al, LSR_THRE
        jz .wait
        mov dx, COM1_DATA
        mov al, ah
        out dx, al
        pop edx
        pop eax
        ret

exc_puthex:
        ;; Low nibble of AL → ASCII hex digit → COM1.
        and al, 0Fh
        cmp al, 10
        jb .digit
        add al, 'A' - 10 - '0'
        .digit:
        add al, '0'
        jmp exc_putc

int30_handler:
        ;; Placeholder pmode INT 30h gate.  The widened syscall handler
        ;; replaces this once cc.py + kernel move to 32-bit (phase 4+).
        ;; For now: print 'S' and halt so we notice if anything fires it.
        mov al, 'S'
        call exc_putc
        cli
        .halt:
        hlt
        jmp .halt

        ;; Exception stub table.  Error-code exceptions on 386: 8, 10..14.
        EXC_NOERR 0
        EXC_NOERR 1
        EXC_NOERR 2
        EXC_NOERR 3
        EXC_NOERR 4
        EXC_NOERR 5
        EXC_NOERR 6
        EXC_NOERR 7
        EXC_ERR   8
        EXC_NOERR 9
        EXC_ERR   10
        EXC_ERR   11
        EXC_ERR   12
        EXC_ERR   13
        EXC_ERR   14
        EXC_NOERR 15
        EXC_NOERR 16
        EXC_NOERR 17
        EXC_NOERR 18
        EXC_NOERR 19
        EXC_NOERR 20
        EXC_NOERR 21
        EXC_NOERR 22
        EXC_NOERR 23
        EXC_NOERR 24
        EXC_NOERR 25
        EXC_NOERR 26
        EXC_NOERR 27
        EXC_NOERR 28
        EXC_NOERR 29
        EXC_NOERR 30
        EXC_NOERR 31

[bits 16]

        ;; ----- IDT data -----
        ;; Vectors 0..31: CPU exceptions. Vectors 32..47: reserved for
        ;; remapped IRQs (filled in later). Vector 0x30: syscall gate.
        align 8
idt_start:
        IDT_ENTRY exc_0
        IDT_ENTRY exc_1
        IDT_ENTRY exc_2
        IDT_ENTRY exc_3
        IDT_ENTRY exc_4
        IDT_ENTRY exc_5
        IDT_ENTRY exc_6
        IDT_ENTRY exc_7
        IDT_ENTRY exc_8
        IDT_ENTRY exc_9
        IDT_ENTRY exc_10
        IDT_ENTRY exc_11
        IDT_ENTRY exc_12
        IDT_ENTRY exc_13
        IDT_ENTRY exc_14
        IDT_ENTRY exc_15
        IDT_ENTRY exc_16
        IDT_ENTRY exc_17
        IDT_ENTRY exc_18
        IDT_ENTRY exc_19
        IDT_ENTRY exc_20
        IDT_ENTRY exc_21
        IDT_ENTRY exc_22
        IDT_ENTRY exc_23
        IDT_ENTRY exc_24
        IDT_ENTRY exc_25
        IDT_ENTRY exc_26
        IDT_ENTRY exc_27
        IDT_ENTRY exc_28
        IDT_ENTRY exc_29
        IDT_ENTRY exc_30
        IDT_ENTRY exc_31
        ;; Vectors 32..47: reserved for IRQs after PIC remap.  Left as
        ;; not-present (all zeros) — a stray IRQ before they're filled in
        ;; will #GP, which lands in exc_common with exception 13.
        times (0x30 - 32) * 8 db 0
        IDT_ENTRY int30_handler         ; vector 0x30
idt_end:

idtr:
        dw idt_end - idt_start - 1
        dd idt_start

idt_install:
        ;; Load IDTR.  Safe to call in real mode (just sets the register);
        ;; the IDT isn't consulted until CR0.PE flips.  Preserves all regs.
        lidt [idtr]
        ret
