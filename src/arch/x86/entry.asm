;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; `boot/stage1_5.asm`'s `enter_protected_mode` far-jumps here after
;;; flipping CR0.PE.  We're now in flat 32-bit ring 0: CS=0x08 (code),
;;; DS=ES=SS=FS=GS=0x10 (data), ESP=0x9FFF0.
;;;
;;; First pmode milestone: halt.  Everything past this point — re-doing
;;; the driver inits that depended on 16-bit BIOS conventions, loading
;;; and running a 32-bit shell, widening the jump-table targets — lands
;;; in follow-up PRs.  Any CPU exception that fires after the flip
;;; vectors through `idt.asm`'s `exc_common` and prints `EXCnn` on COM1.
;;; ------------------------------------------------------------------------

[bits 32]
protected_mode_entry:
        cli
        .halt:
        hlt
        jmp .halt

[bits 16]
