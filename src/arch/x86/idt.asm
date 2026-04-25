;;; ------------------------------------------------------------------------
;;; idt.asm — 32-bit IDT with exception stubs for protected-mode use.
;;;
;;; Exports a statically-built IDT covering vectors 0..31 (CPU exceptions)
;;; and 0x30 (INT 30h syscall gate).  Each exception stub normalizes the
;;; stack (pushes a fake error code when the CPU didn't), pushes the
;;; exception number, and jumps to exc_common which prints "EXCnn\r\n" to
;;; COM1 and halts.  No recovery — a panic is a panic.
;;;
;;; The IDTR is loaded via `lidt [idtr]` in bboeos.asm before the pmode
;;; switch; any fault from that point vectors through our stubs.  PIC remap
;;; is orthogonal (belongs with the pmode switch itself); this module does
;;; not touch the PICs.
;;; ------------------------------------------------------------------------

        IDT_CODE_SELECTOR       equ 08h          ; flat 32-bit code (pmode GDT[1])
        IDT_FLAGS_INT32         equ 8Eh          ; P=1 DPL=0 type=0xE
        LSR_THRE                equ 20h

%macro IDT_ENTRY 1
        ;; We rely on the kernel living entirely in the low 64 KB of
        ;; linear memory (MBR loads at 0x7C00 and the flat binary never
        ;; grows past 0xFFFF), so offset 31:16 is always zero.  Revisit
        ;; if anything ever lives above that line.
        dw %1
        dw IDT_CODE_SELECTOR
        db 0
        db IDT_FLAGS_INT32
        dw 0
%endmacro

%macro EXC_NOERR 1
exc_%1:
        push 0                      ; fake error code
        push %1
        jmp exc_common
%endmacro

%macro EXC_ERR 1
exc_%1:
        push %1                     ; CPU already pushed the real error code
        jmp exc_common
%endmacro

        ;; Exception stub table.  Error-code exceptions on 386+: 8, 10..14, 17.
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
        EXC_ERR   17
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

        ;; ----- IDT data -----
        ;; Vectors 0..31: CPU exceptions. Vectors 32..47: reserved for
        ;; remapped IRQs (filled in later by entry.asm via idt_set_gate32).
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
        IDT_ENTRY syscall_handler       ; vector 0x30 (INT 30h syscall gate)
idt_end:

idtr:
        dw idt_end - idt_start - 1
        dd idt_start

idt_set_gate32:
        ;; Install a 32-bit interrupt gate.  EAX = handler linear address,
        ;; BL = vector.  Writes offset_lo / selector=0x08 / reserved=0 /
        ;; flags=0x8E / offset_hi into `idt_start + vector*8`.  Callers
        ;; are expected to be in protected mode with the IDT live.  This
        ;; is the seam every widened IRQ / syscall handler uses to
        ;; register itself lazily from 32-bit code.
        push edi
        push eax
        movzx edi, bl
        shl edi, 3
        add edi, idt_start
        mov [edi], ax
        mov word [edi + 2], IDT_CODE_SELECTOR
        mov byte [edi + 4], 0
        mov byte [edi + 5], IDT_FLAGS_INT32
        shr eax, 16
        mov [edi + 6], ax
        pop eax
        pop edi
        ret

