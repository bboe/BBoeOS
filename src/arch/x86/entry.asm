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
;;;   4. Clear screen, print welcome banner to COM1 + VGA.
;;;   5. Echo keyboard and serial input back to COM1 + VGA.
;;;
;;; Any CPU exception fired past this point vectors through
;;; `idt.asm`'s `exc_common` and prints `EXCnn` on COM1.
;;; ------------------------------------------------------------------------

        PMODE_IRQ0_VECTOR       equ 0x20        ; matches the pic_remap master base
        PMODE_IRQ6_VECTOR       equ 0x26

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
        mov eax, pmode_irq6_handler
        mov bl, PMODE_IRQ6_VECTOR
        call idt_set_gate32
        call fdc_init
        call ps2_init

        ;; Zero the system tick counter before unmasking IRQ 0.
        mov dword [system_ticks], 0

        ;; Unmask IRQ 0 (PIT) and IRQ 6 (FDC).  IRQ 1 is unmasked by ps2_init.
        in al, PMODE_PIC1_DATA
        and al, 0BEh                    ; clear bits 0, 6 (unmask IRQ 0, IRQ 6)
        out PMODE_PIC1_DATA, al

        sti

        call vga_clear_screen

        ;; Print welcome banner to COM1 and VGA.
        mov esi, WELCOME_MSG
        .banner:
        mov al, [esi]
        test al, al
        jz .banner_done
        call put_character
        inc esi
        jmp .banner
        .banner_done:

        ;; Echo loop: poll keyboard ring buffer and COM1, echo each char.
        .echo_loop:
        call ps2_getc
        test al, al
        jnz .echo_char
        call serial_getc
        test al, al
        jz .echo_loop
        .echo_char:
        call put_character
        jmp .echo_loop

;;; -----------------------------------------------------------------------
;;; serial_getc — non-blocking COM1 read.
;;; Returns: AL = char, or AL=0 (ZF set) if no data.
;;; Translates DEL (0x7F) to BS (0x08) for serial terminal compatibility.
;;; -----------------------------------------------------------------------
serial_getc:
        push edx
        mov dx, COM1_LSR
        in al, dx
        test al, 1                      ; bit 0 = data ready
        jz .no_data
        mov dx, COM1_DATA
        in al, dx
        cmp al, 7Fh                     ; DEL → backspace
        jne .done
        mov al, 08h
        jmp .done
        .no_data:
        xor al, al
        .done:
        pop edx
        ret

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

pmode_irq6_handler:
        ;; FDC command complete.  EOI.
        push eax
        mov al, PMODE_PIC_EOI
        out PMODE_PIC1_CMD, al
        pop eax
        iretd

;;; -----------------------------------------------------------------------
