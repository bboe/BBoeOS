;;; ------------------------------------------------------------------------
;;; syscall.asm — 32-bit INT 30h dispatcher.
;;;
;;; ABI is the 16-bit BBoeOS shape widened to E-regs — i.e. cc.py emits the
;;; same code under --bits 16 and --bits 32, just with E-reg widths under
;;; the 32-bit target:
;;;
;;;   AH         syscall number (see include/constants.asm SYS_*)
;;;   EBX/ECX/   args in syscall-specific positions (BX=fd, SI=path/buf,
;;;     EDX/ESI/   DI=buf, CX=count, AL=flags, etc.)  Each handler below
;;;     EDI        documents what its kernel function expects.
;;;   AX         return value (high 16 bits of saved EAX preserved)
;;;   CF         error flag — handlers leave the kernel's CF intact and the
;;;              dispatcher propagates it to the user's saved EFLAGS.
;;;
;;; Dispatch is a flat jump table indexed by AH.  SYS_* numbers are sparse
;;; (the high nibble groups subsystems — 0x0 fs, 0x1 io, 0x2 net, 0x3 rtc,
;;; 0xF sys), so most of the 0xF4 table entries are `.iret_invalid` fillers
;;; emitted by `times` at each group boundary.  ~1 KB total; the table is
;;; the syscall manifest.
;;;
;;; Frame at the top of `syscall_handler` (after pushad):
;;;   [esp+ 0]  edi          [esp+16]  ebx
;;;   [esp+ 4]  esi          [esp+20]  edx
;;;   [esp+ 8]  ebp          [esp+24]  ecx
;;;   [esp+12]  esp (pre-pushad)
;;;   [esp+28]  eax          ← user's syscall number in AH; AX overwritten
;;;                            with retval, high 16 preserved
;;;   [esp+32]  eip / [esp+36] cs / [esp+40] eflags  (CPU iretd frame)
;;; ------------------------------------------------------------------------

        SYSCALL_COUNT           equ SYS_SYS_SHUTDOWN + 1        ; one past the last valid number
        SYSCALL_SAVED_EAX       equ 28
        SYSCALL_SAVED_EDX       equ 20
        SYSCALL_SAVED_EFLAGS    equ 40

syscall_handler:
        pushad

        ;; AH lives at the second byte of the saved EAX slot.  movzx so the
        ;; jump-table index is a clean 0..255.
        movzx eax, byte [esp + SYSCALL_SAVED_EAX + 1]
        cmp eax, SYSCALL_COUNT
        jae .iret_invalid
        jmp [.table + eax*4]

        .iret_invalid:
        ;; Out-of-range syscall: surface CF=1 and AX=-1 like a kernel error.
        stc
        mov ax, -1
        jmp .iret_cf

        .iret_cf:
        ;; Handlers reach here after their kernel call returns with CF and AX
        ;; carrying the result.  Propagate CF to the user's saved EFLAGS,
        ;; write AX into the low 16 of saved EAX, then iretd.  The high 16
        ;; of saved EAX is left untouched — kernel calls only return 16-bit
        ;; values, and the user's pre-syscall upper bits are theirs.
        jnc .iret_cf_clear
        or dword [esp + SYSCALL_SAVED_EFLAGS], 1
        jmp .iret_cf_write
        .iret_cf_clear:
        and dword [esp + SYSCALL_SAVED_EFLAGS], ~1
        .iret_cf_write:
        mov [esp + SYSCALL_SAVED_EAX], ax
        popad
        iretd

        ;; Each SYS_ENTRY pads with .iret_invalid up to the requested slot,
        ;; then plants the handler pointer.  NASM's `times` refuses a
        ;; negative count, so if a SYS_* constant is moved down or two
        ;; entries collide, the build fails here — the table and the
        ;; SYS_* numbers can't silently drift out of sync.
%macro SYS_ENTRY 2
        times (%1 - ($ - .table) / 4) dd .iret_invalid
        dd %2
%endmacro

        .table:
        SYS_ENTRY SYS_FS_CHMOD,      .fs_chmod
        SYS_ENTRY SYS_FS_MKDIR,      .fs_mkdir
        SYS_ENTRY SYS_FS_RENAME,     .fs_rename
        SYS_ENTRY SYS_FS_RMDIR,      .fs_rmdir
        SYS_ENTRY SYS_FS_UNLINK,     .fs_unlink
        SYS_ENTRY SYS_IO_CLOSE,      .io_close
        SYS_ENTRY SYS_IO_FSTAT,      .io_fstat
        SYS_ENTRY SYS_IO_IOCTL,      .io_ioctl
        SYS_ENTRY SYS_IO_OPEN,       .io_open
        SYS_ENTRY SYS_IO_READ,       .io_read
        SYS_ENTRY SYS_IO_WRITE,      .io_write
        SYS_ENTRY SYS_NET_MAC,       .net_mac
        SYS_ENTRY SYS_NET_OPEN,      .net_open
        SYS_ENTRY SYS_NET_RECVFROM,  .net_recvfrom
        SYS_ENTRY SYS_NET_SENDTO,    .net_sendto
        SYS_ENTRY SYS_RTC_DATETIME,  .rtc_datetime
        SYS_ENTRY SYS_RTC_MILLIS,    .rtc_millis
        SYS_ENTRY SYS_RTC_SLEEP,     .rtc_sleep
        SYS_ENTRY SYS_RTC_UPTIME,    .rtc_uptime
        SYS_ENTRY SYS_SYS_EXEC,      .sys_exec
        SYS_ENTRY SYS_SYS_EXIT,      .sys_exit
        SYS_ENTRY SYS_SYS_REBOOT,    .sys_reboot
        SYS_ENTRY SYS_SYS_SHUTDOWN,  .sys_shutdown

        ;; Handler subfiles — kept at the bottom so the dispatch logic and
        ;; the manifest read top-to-bottom before the per-case bodies.
        ;; Each subfile uses only local labels (`.fs_chmod`, `.io_write`,
        ;; …) so they attach to syscall_handler's scope; if a subfile ever
        ;; needs a named helper or private data, switch the table to refer
        ;; to global labels (sys_fs_chmod, etc.) instead.
%include "syscall/fs.asm"
%include "syscall/io.asm"
%include "syscall/net.asm"
%include "syscall/rtc.asm"
%include "syscall/sys.asm"
