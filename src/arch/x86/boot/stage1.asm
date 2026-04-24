;;; ------------------------------------------------------------------------
;;; stage1.asm — 512-byte MBR.
;;;
;;; Minimal boot loader: set up segments + stack, load stage 2 from the
;;; boot device via BIOS INT 13h, jump into stage 2.  All kernel init
;;; (pic_remap, rtc_tick_init, install_syscalls, network_initialize) and
;;; the user-visible welcome message run from stage 2's `boot_shell` so
;;; they can use the full console driver (drivers/ansi.asm) instead of
;;; something that has to fit inside 512 bytes.
;;;
;;; On disk error we print a single '!' via INT 10h AH=0Eh and halt; the
;;; character is enough to distinguish "MBR booted but couldn't read
;;; stage 2" from "MBR didn't load at all" without pulling a string
;;; printer into the MBR.
;;; ------------------------------------------------------------------------

start:
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov [boot_disk], dl

        ;; Dedicated stack at SS=0x9000, SP=0xFFF0 (linear 0x90000-0x9FFF0)
        ;; owns its entire segment and can never collide with the
        ;; kernel, disk buffers, or loaded programs in segment 0.
        cli
        mov ax, 9000h
        mov ss, ax
        mov sp, 0FFF0h
        sti

        ;; Reset disk controllers before the first read; defensive on
        ;; real hardware, no-op on QEMU.
        xor ax, ax
        int 13h
        jc .error

        ;; Read STAGE2_SECTORS sectors at CHS (cyl=0, head=0, sector=2)
        ;; into linear 0x7E00 (segment 0, offset 0x7E00 via ES=0 / BX).
        mov ax, 0200h | STAGE2_SECTORS
        mov bx, 7E00h
        mov cx, 2
        mov dh, 0
        mov dl, [boot_disk]
        int 13h
        jc .error
        cmp al, STAGE2_SECTORS
        jne .error

        jmp boot_shell

        .error:
        mov ax, 0E00h | '!'
        xor bx, bx
        int 10h
        .halt:
        hlt
        jmp .halt

        boot_disk db 0

        times 510-($-$$) db 0
        dw 0AA55h
