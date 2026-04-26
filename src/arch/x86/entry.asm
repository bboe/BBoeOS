;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; Far-jumped to from bboeos.asm after flipping CR0.PE.  We're now in
;;; flat 32-bit ring 0: CS=0x08 (code), DS=ES=SS=FS=GS=0x10 (data),
;;; ESP=0x9FFF0.
;;;
;;; protected_mode_entry runs once per boot — segment reload, PIT + IRQ
;;; install, driver/vfs init, banner — then falls through into
;;; shell_reload.  shell_reload is the re-entry point for SYS_EXIT: it
;;; reloads the program binary off disk and `jmp`s program_enter, which
;;; resets fds, zeros BSS, snapshots ESP, and jumps into PROGRAM_BASE.
;;;
;;; Any CPU exception fired past this point vectors through
;;; `idt.asm`'s `exc_common` and prints `EXCnn` on COM1.
;;; ------------------------------------------------------------------------

        PMODE_IRQ0_VECTOR       equ 0x20        ; matches the pic_remap master base
        PMODE_IRQ6_VECTOR       equ 0x26

bss_setup:
        ;; Zero the BSS region of the freshly-loaded program at
        ;; PROGRAM_BASE.  Reads binary size from vfs_found_size, checks
        ;; for the 4-byte trailer (dw bss_size; dw BSS_MAGIC) at the end
        ;; of the binary, then zeroes bss_size bytes immediately after.
        ;; Same protocol as the 16-bit kernel — programs end with an
        ;; explicit trailer if they want their BSS zeroed.
        push eax
        push ecx
        push edi
        movzx ecx, word [vfs_found_size]        ; binary size (low 16)
        mov edi, PROGRAM_BASE
        add edi, ecx                            ; EDI = PROGRAM_BASE + binary_size
        cmp ecx, 4
        jb .bss_done
        cmp word [edi - 2], BSS_MAGIC
        jne .bss_done
        movzx ecx, word [edi - 4]               ; BSS byte count
        test ecx, ecx
        jz .bss_done
        xor eax, eax
        cld
        rep stosb
        .bss_done:
        pop edi
        pop ecx
        pop eax
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

program_enter:
        ;; jmp target — never call.  Caller has vfs_load'd a program at
        ;; PROGRAM_BASE; this resets the fd table, zeros the program's
        ;; BSS region per the trailer-magic protocol, snapshots the
        ;; kernel ESP into [shell_esp], and jumps into the program.
        ;; sys_exit teleports back to the snapshot regardless of how
        ;; the program mangled its stack.  Using `jmp` (not `call`)
        ;; means SYS_EXIT respawns don't leave stranded return
        ;; addresses on the kernel stack.
        call fd_init
        call bss_setup
        mov [shell_esp], esp
        jmp PROGRAM_BASE

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
        call ata_init
        call fd_init
        call fdc_init
        call ps2_init
        call vfs_init
        ;; Probe the NE2000 NIC and bring it up if present.  CF set =
        ;; no NIC, which is fine — netinit / net programs surface that
        ;; via a "no NIC" message rather than halting the kernel.
        call network_initialize

        ;; Zero the system tick counter before unmasking IRQ 0.
        mov dword [system_ticks], 0

        ;; Unmask IRQ 0 (PIT) and IRQ 6 (FDC).  IRQ 1 is unmasked by ps2_init.
        in al, PMODE_PIC1_DATA
        and al, 0BEh                    ; clear bits 0, 6 (unmask IRQ 0, IRQ 6)
        out PMODE_PIC1_DATA, al

        sti

        call vga_clear_screen

        ;; Print welcome banner to COM1 and VGA.
        mov esi, welcome_msg
        .banner:
        mov al, [esi]
        test al, al
        jz .banner_done
        call put_character
        inc esi
        jmp .banner
        .banner_done:
        ;; Fall through into shell_reload.

shell_reload:
        ;; Reload bin/shell off disk and run it.  Same lifecycle as
        ;; sys_exec's .exec_load: find → load → program_enter.
        mov esi, shell_path
        call vfs_find
        jc .shell_fail
        mov edi, PROGRAM_BASE
        call vfs_load
        jc .shell_fail
        jmp program_enter

        .shell_fail:
        ;; Missing or unreadable program.  Halt — no sensible recovery
        ;; here yet, and the error is noisy enough via exc_common if a
        ;; #PF or #GP fires instead.
        cli
        hlt
        jmp $-1

shell_esp       dd 0            ; kernel ESP snapshot, restored by sys_exit
shell_path      db "bin/shell", 0
welcome_msg     db "Welcome to BBoeOS!", 13, 10, "Version 0.7.0 (2026/04/23)", 13, 10, 0

;;; -----------------------------------------------------------------------
