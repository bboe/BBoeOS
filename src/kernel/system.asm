reboot:
        ;; Pulse the 8042 reset line.  Drain the input buffer first so the
        ;; 0xFE command isn't dropped, then halt in case the reset lags.
        cli
        .wait_idle:
        in al, 64h
        test al, 02h
        jnz .wait_idle
        mov al, 0FEh
        out 64h, al
        .hang:
        hlt
        jmp .hang

shutdown:
        ;; Try QEMU ACPI shutdown (PIIX4 PM control port)
        mov dx, 0604h
        mov ax, 2000h
        out dx, ax

        ;; Try Bochs/old QEMU shutdown port
        mov dx, 0B004h
        mov ax, 2000h
        out dx, ax

        ;; If still running, shutdown is not supported
        ret
