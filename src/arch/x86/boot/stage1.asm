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

        ;; Read stage2 at CHS (cyl=0, head=0, sector=2) into linear 0x7E00.
        ;; The byte count lives in `stage2_bytes` (NASM-computed from
        ;; kernel_end - 7E00h and placed at MBR offset 508), so host tools
        ;; can read the same value from the drive image.  Here we shift right
        ;; by 9 to get the sector count, and publish `directory_sector` =
        ;; stage2_sectors + 1 for bbfs / ext2 to consume.
        mov ax, [stage2_bytes]
        add ax, 511
        shr ax, 9
        mov [directory_sector], ax
        inc word [directory_sector]
        mov ah, 02h             ; BIOS read-sectors function (AL = count)
        mov bx, 7E00h
        mov cx, 2
        mov dh, 0
        mov dl, [boot_disk]
        int 13h
        jc .error

        jmp boot_shell

        .error:
        mov ax, 0E00h | '!'
        xor bx, bx
        int 10h
        .halt:
        hlt
        jmp .halt

        boot_disk db 0
        directory_sector dw 0           ; stage2_sectors + 1; set at boot, read by bbfs

        times 508-($-$$) db 0
        stage2_bytes dw kernel_end - 7E00h      ; fixed offset 508; host tools depend on it
        dw 0AA55h
