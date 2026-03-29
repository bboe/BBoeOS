ne2k_probe:
        ;; Probe and reset NE2000 NIC, read MAC address into mac_addr
        ;; Output: CF clear on success, CF set on failure (no NIC or timeout)
        push ax
        push cx
        push dx
        push di

        ;; Reset the NIC
        mov dx, NE2K_BASE + 1Fh ; Reset port
        in al, dx
        out dx, al              ; Write back to trigger reset

        ;; Wait for ISR reset bit (bit 7)
        mov cx, 0FFFFh
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_reset:
        in al, dx
        test al, 80h           ; RST bit
        jnz .reset_done
        loop .wait_reset
        stc                    ; Timeout — no NIC found
        jmp .probe_done

        .reset_done:
        ;; Acknowledge all interrupts
        mov al, 0FFh
        out dx, al

        ;; Stop the NIC: page 0, stop, abort DMA
        mov dx, NE2K_BASE      ; CR
        mov al, 21h
        out dx, al

        ;; Verify NIC exists by reading back CR
        in al, dx
        and al, 3Fh            ; Mask off page select bits
        cmp al, 21h
        jne .probe_fail

        ;; NE2000 register reference:
        ;; Datasheet: https://media.digikey.com/pdf/Data%20Sheets/Texas%20Instruments%20PDFs/DP8390D,NS32490D.pdf
        ;; OSDev wiki: https://wiki.osdev.org/Ne2000

        ;; Data configuration: word-wide DMA, normal mode, 4-byte FIFO
        mov dx, NE2K_BASE + 0Eh ; DCR
        mov al, 49h
        out dx, al

        ;; Clear remote byte count
        mov dx, NE2K_BASE + 0Ah ; RBCR0
        xor al, al
        out dx, al
        inc dx                 ; RBCR1
        out dx, al

        ;; Monitor mode — don't accept packets during probe
        mov dx, NE2K_BASE + 0Ch ; RCR
        mov al, 20h
        out dx, al

        ;; Internal loopback
        mov dx, NE2K_BASE + 0Dh ; TCR
        mov al, 02h
        out dx, al

        ;; Read 32 bytes of PROM via remote DMA
        mov dx, NE2K_BASE + 08h ; RSAR0
        xor al, al
        out dx, al             ; Remote start address low = 0
        inc dx                 ; RSAR1
        out dx, al             ; Remote start address high = 0

        mov dx, NE2K_BASE + 0Ah ; RBCR0
        mov al, 20h            ; 32 bytes
        out dx, al
        inc dx                 ; RBCR1
        xor al, al
        out dx, al

        mov dx, NE2K_BASE      ; CR
        mov al, 0Ah            ; Start + Remote Read DMA
        out dx, al

        ;; Read 6 MAC bytes (word mode: low byte of each word is the MAC byte)
        mov di, mac_addr
        mov cx, 6
        mov dx, NE2K_BASE + 10h ; Data port
        cld
        .read_mac:
        in ax, dx
        stosb                  ; Store low byte
        loop .read_mac

        ;; Drain remaining 10 words to complete the 32-byte DMA transfer
        mov cx, 10
        .drain:
        in ax, dx
        loop .drain

        ;; Wait for remote DMA complete (ISR RDC bit)
        mov dx, NE2K_BASE + 07h ; ISR
        .wait_dma:
        in al, dx
        test al, 40h           ; RDC bit
        jz .wait_dma
        mov al, 40h
        out dx, al             ; Acknowledge RDC

        clc                    ; Success
        jmp .probe_done

        .probe_fail:
        stc

        .probe_done:
        pop di
        pop dx
        pop cx
        pop ax
        ret

        ;; Variables
        mac_addr times 6 db 0
