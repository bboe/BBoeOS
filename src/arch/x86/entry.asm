;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; Far-jumped to from bboeos.asm after flipping CR0.PE.  We're now in
;;; flat 32-bit ring 0: CS=0x08 (code), DS=ES=SS=FS=GS=0x10 (data),
;;; ESP=0x9FFF0.
;;;
;;; Steady-state responsibilities:
;;;   1. Reload segment registers and stack.
;;;   2. Reprogram PIT to 100 Hz and install 32-bit IRQ handlers.
;;;   3. `sti` so those IRQs actually fire (uptime clock starts here).
;;;   4. Initialize VGA text mode and print welcome banner to COM1 + VGA.
;;;   5. Halt.
;;;
;;; Any CPU exception fired past this point vectors through
;;; `idt.asm`'s `exc_common` and prints `EXCnn` on COM1.
;;; ------------------------------------------------------------------------

PMODE_IRQ0_VECTOR       equ 0x20    ; matches the pic_remap master base
PMODE_IRQ1_VECTOR       equ 0x21
PMODE_IRQ6_VECTOR       equ 0x26
PMODE_PIC1_CMD          equ 0x20
PMODE_PIC1_DATA         equ 0x21
PMODE_PIC_EOI           equ 0x20
PMODE_PS2_DATA          equ 0x60

WELCOME_MSG db "Welcome to BBoeOS!", 13, 10, "Version 0.7.0 (2026/04/23)", 13, 10, 0

protected_mode_entry:
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov esp, 0x9FFF0

        ;; Reprogram PIT to 100 Hz (MS_PER_TICK=10 ms/tick).
        ;; Constants defined in drivers/rtc.asm, assembled before entry.asm.
        mov al, PIT_MODE2_LOHI_CH0
        out PIT_COMMAND, al
        mov al, PIT_DIVISOR & 0xFF
        out PIT_CHANNEL0, al
        mov al, PIT_DIVISOR >> 8
        out PIT_CHANNEL0, al

        ;; Install 32-bit IRQ handlers.
        mov eax, pmode_irq0_handler
        mov bl, PMODE_IRQ0_VECTOR
        call idt_set_gate32
        mov eax, pmode_irq1_handler
        mov bl, PMODE_IRQ1_VECTOR
        call idt_set_gate32
        mov eax, pmode_irq6_handler
        mov bl, PMODE_IRQ6_VECTOR
        call idt_set_gate32

        ;; Zero the system tick counter before unmasking IRQ 0.
        mov dword [system_ticks], 0

        ;; Unmask IRQ 0 (PIT), IRQ 1 (PS/2 keyboard), and IRQ 6 (FDC).
        ;; Bit 0 = IRQ 0, bit 1 = IRQ 1, bit 6 = IRQ 6.
        in al, PMODE_PIC1_DATA
        and al, 0BAh                    ; clear bits 0, 1, 6
        out PMODE_PIC1_DATA, al

        sti

        ;; Print welcome banner to COM1 via serial_character.
        ;; VGA init deferred until vga.asm is ported to 32-bit flat addressing.
        mov esi, WELCOME_MSG
        .banner:
        mov al, [esi]
        test al, al
        jz .banner_done
        call serial_character
        inc esi
        jmp .banner
        .banner_done:

        .halt:
        hlt
        jmp .halt

pmode_irq0_handler:
        ;; PIT tick.  Increment `system_ticks` (dword in rtc.asm's
        ;; data region, reachable via flat DS), EOI the master PIC,
        ;; iretd.  Interrupt gate entry leaves IF=0 for the body, so
        ;; the `inc dword [mem]` is safe against reentrancy; on a
        ;; single CPU we don't need the LOCK prefix.
        push eax
        inc dword [system_ticks]
        mov al, PMODE_PIC_EOI
        out PMODE_PIC1_CMD, al
        pop eax
        iretd

pmode_irq1_handler:
        ;; PS/2 keyboard.  Consume the scancode from port 0x60 (the
        ;; controller's output buffer stays asserted until read,
        ;; which keeps the IRQ line stuck high), EOI, iretd.  We
        ;; discard the scancode for now — the consumer (input queue,
        ;; scancode-to-ASCII translation) comes back as the console /
        ;; shell widen.
        push eax
        in al, PMODE_PS2_DATA
        mov al, PMODE_PIC_EOI
        out PMODE_PIC1_CMD, al
        pop eax
        iretd

pmode_irq6_handler:
        ;; FDC command complete.  EOI.
        push eax
        mov al, PMODE_PIC_EOI
        out PMODE_PIC1_CMD, al
        pop eax
        iretd
