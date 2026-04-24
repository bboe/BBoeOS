;;; ------------------------------------------------------------------------
;;; init.asm — kernel subsystem initialization.
;;;
;;; Called once by boot_shell before the shell is loaded.  Keeps the init
;;; sequence encapsulated so the pmode port can refactor it (some steps
;;; will move to post-flip once the 32-bit IDT is live) without churning
;;; stage2.asm.
;;; ------------------------------------------------------------------------

kernel_init:
        call pic_remap          ; master → 0x20..0x27, slave → 0x28..0x2F, all masked
        call rtc_tick_init      ; install IRQ 0 handler, zero system_ticks
        call install_syscalls
        call network_initialize ; probe NIC once; sets net_present on success
        ret
