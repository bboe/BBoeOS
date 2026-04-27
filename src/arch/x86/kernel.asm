;;; -----------------------------------------------------------------------
;;; kernel.asm — aggregate every kernel subsystem into the flat binary.
;;;
;;; bboeos.asm %includes this file once, immediately after stage2.asm, so
;;; all the code the bootloader hands off to sits contiguously after the
;;; boot handoff.  Nothing here has a required physical offset — labels
;;; resolve via nasm's two-pass assembly — so the ordering below is only
;;; for reader legibility.
;;;
;;; Boundary: arch-specific code lives in src/arch/x86/; everything else
;;; (drivers, fs, net, lib, syscall handlers) is top-level and reusable
;;; across architectures.  A hypothetical arch/x86_64/kernel.asm would
;;; list the same top-level subsystems but its own arch-specific bits.
;;; -----------------------------------------------------------------------

        ;; Hardware drivers
%include "drivers/ansi.asm"             ; put_character / serial_character (console)
%include "drivers/ata.asm"              ; IDE ATA PIO disk driver
%include "drivers/fdc.asm"              ; floppy DMA + IRQ 6 driver
%include "drivers/ne2k.asm"             ; NE2000 ISA NIC (polled)
%include "drivers/ps2.asm"              ; PS/2 keyboard driver
%include "drivers/rtc.asm"              ; CMOS RTC + PIT tick counter
%include "drivers/vga.asm"              ; VGA text + mode-13h helpers

        ;; File descriptors, block I/O dispatch, filesystems, VFS switch
%include "fs/fd.kasm"                   ; fd table + per-type backends
%include "fs/block.asm"                 ; read_sector / write_sector dispatch
%include "fs/vfs.asm"                   ; VFS layer (includes fs/bbfs.asm + fs/ext2.asm)

        ;; Shared library utilities used by programs via the jump table
%include "lib/lib.asm"                  ; lib/print.asm + lib/proc.asm

        ;; Network protocol stack (NIC driver lives above in drivers/ne2k.asm)
%include "net/net.kasm"                 ; net/arp.c + icmp.c + ip.c + udp.c (compiled)

        ;; x86 / PC: 8259 PIC, INT 30h dispatcher, reboot / shutdown, kernel init
%include "pic.asm"                      ; pic_remap
%include "syscall.asm"                  ; INT 30h dispatcher + syscall/ handlers
%include "system.asm"                   ; reboot (8042), shutdown (QEMU/ACPI)
%include "init.asm"                     ; kernel_init (called from boot_shell)
