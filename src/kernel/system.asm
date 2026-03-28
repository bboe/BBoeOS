reboot:
        int 19h                 ; Bootstrap loader — re-reads and executes boot sector
        ret

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
