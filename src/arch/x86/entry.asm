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
        ;; for either the new 6-byte trailer (dd bss_size; dw
        ;; BSS_MAGIC32) or the legacy 4-byte trailer (dw bss_size; dw
        ;; BSS_MAGIC) at the end of the binary, then zeroes bss_size
        ;; bytes immediately after.  Programs end with an explicit
        ;; trailer if they want their BSS zeroed.
        push eax
        push ecx
        push edi
        movzx ecx, word [vfs_found_size]        ; binary size (low 16)
        mov edi, PROGRAM_BASE
        add edi, ecx                            ; EDI = PROGRAM_BASE + binary_size
        cmp ecx, 6
        jb .try_old
        cmp word [edi - 2], BSS_MAGIC32
        jne .try_old
        mov ecx, [edi - 6]                      ; 32-bit BSS byte count
        jmp .zero
        .try_old:
        cmp word [edi - 2], BSS_MAGIC
        jne .bss_done
        movzx ecx, word [edi - 4]               ; legacy 16-bit BSS byte count
        .zero:
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
        ;; kernel ESP into [shell_esp], and `iretd`s into the program at
        ;; CPL=3.  sys_exit teleports back to the snapshot regardless of
        ;; how the program mangled its stack.  Using `jmp` (not `call`)
        ;; from shell_reload / sys_exec means SYS_EXIT respawns don't
        ;; leave stranded return addresses on the kernel stack.
        call fd_init
        call bss_setup
        mov [shell_esp], esp

        ;; Drop into ring 3.  iretd loads SS, ESP, EFLAGS, CS, EIP
        ;; atomically from the constructed frame, so we never `mov ss,
        ;; ax` directly — that would split the SS:ESP load across two
        ;; instructions and an interrupt between them would push to the
        ;; wrong stack.  iretd does NOT reload DS/ES/FS/GS, so do those
        ;; by hand first; once they hold a DPL=3 selector the kernel can
        ;; still read/write through them at CPL=0 (CPL ≤ DPL on access).
        mov ax, USER_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov fs, ax
        mov gs, ax
        push dword USER_DATA_SELECTOR   ; SS
        push dword USER_STACK_TOP       ; ESP
        push dword 0x202                ; EFLAGS: IF=1, IOPL=0, reserved bit 1=1
        push dword USER_CODE_SELECTOR   ; CS
        push dword PROGRAM_BASE         ; EIP
        iretd

protected_mode_entry:
        mov ax, 0x10
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov esp, KERNEL_STACK_TOP

        ;; Patch the TSS descriptor's base bytes with tss_data's linear
        ;; address (the bytes are scattered across descriptor offsets
        ;; +2/+4/+7 so we can't fold them at assemble time without
        ;; line-noise expressions), populate the TSS fields the CPU
        ;; consults on a ring-3 → ring-0 transition (SS0, ESP0), parking
        ;; the I/O permission bitmap past the TSS limit so all I/O ports
        ;; trap from CPL=3.  Then `ltr` — must complete before any ring
        ;; transition can fire, but exceptions and IRQs at CPL=0 don't
        ;; need the TSS, so doing it before the rest of init is safe.
        mov eax, tss_data
        mov [gdt_tss + 2], ax
        shr eax, 16
        mov [gdt_tss + 4], al
        mov [gdt_tss + 7], ah
        mov dword [tss_data + 4], KERNEL_STACK_TOP      ; ESP0
        mov word [tss_data + 8], 0x10                   ; SS0 = kernel data
        mov word [tss_data + 102], 104                  ; IOPB offset = TSS limit + 1 → no I/O bitmap
        mov ax, TSS_SELECTOR
        ltr ax

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

        ;; Zero the system tick counter and unmask IRQ 0 (PIT) before
        ;; the driver inits run, so any timing primitive that runs
        ;; during init (e.g. fdc_motor_start's rtc_sleep_ms during
        ;; vfs_init's first read on a floppy boot) sees ticks
        ;; advancing.  IRQ 0 needs an unmasked PIC slot AND ``sti``
        ;; AND a properly-installed handler — all three are true here.
        ;; Other IRQs are unmasked by their own driver inits (IRQ 1
        ;; by ps2_init, IRQ 6 by fdc_init).
        mov dword [system_ticks], 0
        in al, PMODE_PIC1_DATA
        and al, 0FEh                    ; clear bit 0 (unmask IRQ 0)
        out PMODE_PIC1_DATA, al
        sti

        ;; Install the vDSO blob at FUNCTION_TABLE so user programs can
        ;; call FUNCTION_DIE / FUNCTION_PRINT_STRING / etc. before the
        ;; first shell_reload.
        call vdso_install
        call ata_init
        call fd_init
        call fdc_init
        call ps2_init
        call vfs_init
        ;; Probe the NE2000 NIC and bring it up if present.  CF set =
        ;; no NIC, which is fine — netinit / net programs surface that
        ;; via a "no NIC" message rather than halting the kernel.
        call network_initialize

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

vdso_install:
        ;; Copy the embedded vDSO blob (vdso_image..vdso_image_end, 4 KB)
        ;; to physical FUNCTION_TABLE (0x08046000).  User programs `call`
        ;; FUNCTION_DIE / FUNCTION_PRINT_STRING / etc. and land in this
        ;; blob.  Pre-paging the virt = phys identity holds, so the
        ;; programs running at PROGRAM_BASE see the vDSO as ordinary RAM.
        ;; Once paging lands, the kernel will map this same physical
        ;; frame as a user-readable code page in every PD instead.
        push esi
        push edi
        push ecx
        mov esi, vdso_image
        mov edi, FUNCTION_TABLE
        mov ecx, (vdso_image_end - vdso_image) / 4
        cld
        rep movsd
        pop ecx
        pop edi
        pop esi
        ret

shell_esp       dd 0            ; kernel ESP snapshot, restored by sys_exit
shell_path      db "bin/shell", 0
welcome_msg     db "Welcome to BBoeOS!", 13, 10, "Version 0.8.0 (2026/04/27)", 13, 10, 0

        ;; 32-bit TSS.  Only SS0/ESP0/IOPB-offset are populated (in
        ;; protected_mode_entry); all other fields stay zero because we
        ;; don't use hardware task switching.  Sized to the 104-byte
        ;; standard layout so the IOPB-past-limit trick parks I/O.
        align 4
tss_data:
        times 104 db 0

;;; -----------------------------------------------------------------------
