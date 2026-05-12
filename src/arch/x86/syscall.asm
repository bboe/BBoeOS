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
;;; 0xF sys), so most of the 0xF5 table entries are `.iret_invalid` fillers
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

        SYSCALL_COUNT           equ SYS_SYS_SIGRETURN + 1       ; one past the highest SYS_* — bound for the dispatcher range check
        SYSCALL_SAVED_EAX       equ 28
        SYSCALL_SAVED_EDX       equ 20
        SYSCALL_SAVED_EFLAGS    equ 40

syscall_handler:
        pushad

        ;; AH lives at the second byte of the saved EAX slot.  movzx so the
        ;; jump-table index is a clean 0..255.  Look up the handler address,
        ;; push it on the stack, then restore EAX from the saved slot so
        ;; handlers see the user's full EAX (specifically AL — fs_chmod /
        ;; io_open / io_ioctl / net_open all read flags from AL).  ret
        ;; pops the handler address and jumps to it.
        movzx eax, byte [esp + SYSCALL_SAVED_EAX + 1]
        cmp eax, SYSCALL_COUNT
        jae .iret_invalid
        ;; Resolve the handler address into a scratch reg that pushad saved
        ;; (EBP), then restore the user's AL so handlers see the cmd/flags
        ;; byte they document — not the syscall number we used for dispatch.
        mov ebp, [.table + eax*4]
        mov al, [esp + SYSCALL_SAVED_EAX]
        jmp ebp

        .iret_invalid:
        ;; Out-of-range syscall: surface CF=1 and AX=-1 like a kernel error.
        stc
        mov ax, -1
        jmp .iret_cf

        .iret_cf:
        ;; Handlers reach here after their kernel call returns with CF and AX
        ;; carrying the result.  Sign-extend AX into EAX so 32-bit user code
        ;; can compare the result directly (AX=-1 → EAX=-1 for error tests,
        ;; AX=0 → EAX=0 for EOF tests), then propagate CF and iretd.
        movsx eax, ax
        ;; Fall through to .iret_cf_eax — handlers returning a full 32-bit
        ;; value in EAX (io_read / io_write byte counts; io_seek / sys_break
        ;; addresses; rtc_datetime / rtc_millis / rtc_uptime monotonic
        ;; counters; io_ioctl per-cmd values) prepare EAX themselves and
        ;; ``jmp .iret_cf_eax`` to skip the sign-extend.
        .iret_cf_eax:
        jnc .iret_cf_clear
        or dword [esp + SYSCALL_SAVED_EFLAGS], 1
        jmp .iret_cf_write
        .iret_cf_clear:
        and dword [esp + SYSCALL_SAVED_EFLAGS], ~1
        .iret_cf_write:
        mov [esp + SYSCALL_SAVED_EAX], eax
        SIGNAL_TAIL_CHECK
        ;; Update TSS.ESP0 to the current slot's kernel-stack top.  After
        ;; a syscall that yielded mid-flight (e.g. fd_read_pipe), the
        ;; resuming slot may differ from the slot that entered the
        ;; dispatcher — the next int 30h from this slot must land on
        ;; the slot's own kernel stack.  Cheap when the slot didn't
        ;; change.  Preserves all GP regs so popad's restored values
        ;; survive into the user iretd frame.
        call tss_set_esp0_for_current_slot
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
        SYS_ENTRY SYS_IO_DUP,        .io_dup
        SYS_ENTRY SYS_IO_DUP2,       .io_dup2
        SYS_ENTRY SYS_IO_FSTAT,      .io_fstat
        SYS_ENTRY SYS_IO_IOCTL,      .io_ioctl
        SYS_ENTRY SYS_IO_OPEN,       .io_open
        SYS_ENTRY SYS_IO_READ,       .io_read
        SYS_ENTRY SYS_IO_SEEK,       .io_seek
        SYS_ENTRY SYS_IO_WRITE,      .io_write
        SYS_ENTRY SYS_NET_MAC,       .net_mac
        SYS_ENTRY SYS_NET_OPEN,      .net_open
        SYS_ENTRY SYS_NET_RECVFROM,  .net_recvfrom
        SYS_ENTRY SYS_NET_SENDTO,    .net_sendto
        SYS_ENTRY SYS_RTC_ALARM,     .rtc_alarm
        SYS_ENTRY SYS_RTC_DATETIME,  .rtc_datetime
        SYS_ENTRY SYS_RTC_MILLIS,    .rtc_millis
        SYS_ENTRY SYS_RTC_SLEEP,     .rtc_sleep
        SYS_ENTRY SYS_RTC_UPTIME,    .rtc_uptime
        SYS_ENTRY SYS_VIDEO_MAP,     .video_map
        SYS_ENTRY SYS_SYS_BREAK,     .sys_break
        SYS_ENTRY SYS_SYS_EXEC,      .sys_exec
        SYS_ENTRY SYS_SYS_EXIT,      .sys_exit
        SYS_ENTRY SYS_SYS_REBOOT,    .sys_reboot
        SYS_ENTRY SYS_SYS_SHUTDOWN,  .sys_shutdown
        SYS_ENTRY SYS_SYS_SIGNAL,    .sys_signal
        SYS_ENTRY SYS_SYS_SIGRETURN, .sys_sigreturn

        ;; Per-case handler bodies follow.  All but the four net_*
        ;; handlers are inlined here — each one is just a `call
        ;; <existing_function>; jmp .iret_cf` pair (or a few extra
        ;; `mov [esp+N], reg` for syscalls returning DX:AX), so a
        ;; separate %include subfile gained nothing.  The net_*
        ;; handlers — fd-table inspection, per-protocol dispatch,
        ;; payload memcpy through SECTOR_BUFFER — port to C in
        ;; `src/syscall/syscalls.c` and the table entries below are
        ;; thin shims that call them.

        ;; ------------------------------------------------------------
        ;; Filesystem handlers.  cc.py loads args directly into the
        ;; regs each kernel vfs_* helper expects (SI=path, DI=second-
        ;; path, AL=flags).
        ;;
        ;; Handlers that take a user pointer call access_ok_string
        ;; (paths are NUL-terminated within MAX_PATH bytes) before
        ;; dispatching.  A bad pointer surfaces as CF=1, AL=ERROR_FAULT
        ;; so the user sees an errno-style failure rather than the
        ;; kernel ever dereferencing it.  The CPL=0 #PF kill path in
        ;; idt.asm catches the residual case where access_ok passes
        ;; but the user page is unmapped.
        ;;
        ;; fs_chmod / fs_rename / fs_unlink each guard the shell binary
        ;; against being modified, renamed out from under us, or
        ;; deleted.  The check keys on the literal path "bin/shell" —
        ;; aliases like "./bin/shell" still slip through, same as the
        ;; original.
        ;; ------------------------------------------------------------

        .check_shell:
        ;; Returns ZF set if ESI points to the shell path (null-terminated).
        ;; Preserves ESI/EDI/ECX.  Local to the fs group.
        push esi
        push edi
        push ecx
        cld
        mov edi, .shell_name
        mov ecx, .shell_name_len
        repe cmpsb
        pop ecx
        pop edi
        pop esi
        ret

        .check_path:
        ;; Validates ESI as a null-terminated user path within MAX_PATH
        ;; bytes via access_ok_string.  Preserves all caller registers.
        ;; CF=0 on success, CF=1 on bad pointer (handlers should jump
        ;; to .fs_bad_pointer to translate that into AL=ERROR_FAULT).
        push ecx
        mov ecx, MAX_PATH
        call access_ok_string
        pop ecx
        ret

        .fs_chmod:
        ;; SI = path, AL = flags.
        call .check_path
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_chmod_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_chmod_do:
        call vfs_chmod
        jmp .iret_cf

        .fs_mkdir:
        ;; SI = name.  vfs_mkdir returns AX = new sector on success.
        call .check_path
        jc .fs_bad_pointer
        call vfs_mkdir
        jmp .iret_cf

        .fs_rename:
        ;; SI = old path, DI = new path.  Validate both pointers, then
        ;; guard the shell as the rename source only — vfs_rename's
        ;; own "destination exists" check refuses overwriting bin/shell.
        call .check_path
        jc .fs_bad_pointer
        xchg esi, edi
        call .check_path
        xchg esi, edi
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_rename_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_rename_do:
        call vfs_rename
        jmp .iret_cf

        .fs_rmdir:
        ;; SI = path.
        call .check_path
        jc .fs_bad_pointer
        call vfs_rmdir
        jmp .iret_cf

        .fs_unlink:
        ;; SI = path.
        call .check_path
        jc .fs_bad_pointer
        call .check_shell
        jne .fs_unlink_do
        mov al, ERROR_PROTECTED
        stc
        jmp .iret_cf
        .fs_unlink_do:
        call vfs_delete
        jmp .iret_cf

        .fs_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        .shell_name            db "bin/shell", 0
        .shell_name_len        equ $ - .shell_name

        ;; ------------------------------------------------------------
        ;; I/O handlers.  cc.py loads args directly into the regs each
        ;; fd_* helper expects (BX=fd, AL=flags/cmd, SI=buf for write,
        ;; DI=buf for read, CX=count).
        ;; ------------------------------------------------------------

        .io_close:
        ;; BX = fd.
        call fd_close
        jmp .iret_cf

        .io_dup:
        call fd_dup
        jmp .iret_cf_eax

        .io_dup2:
        call fd_dup2
        jmp .iret_cf_eax

        .io_fstat:
        ;; BX = fd.  fd_fstat returns AL = mode, CX:DX = size (32-bit).
        ;; Mirror the asm version: write CX/DX into the saved-regs
        ;; slots so the user sees mode in AL and size in CX:DX after
        ;; iret.  Saved CX is at SAVED_EAX-4 (24), saved DX at
        ;; SAVED_EAX-8 (20).
        call fd_fstat
        jc .iret_cf
        mov [esp + SYSCALL_SAVED_EDX], dx
        mov [esp + SYSCALL_SAVED_EDX + 4], cx
        jmp .iret_cf

        .io_ioctl:
        ;; BX = fd, AL = cmd, other regs per (fd_type, cmd).  Use
        ;; .iret_cf_eax (full 32-bit return) rather than .iret_cf
        ;; (movsx ax->eax) so per-fd-type handlers that report values
        ;; wider than 16 bits get them through to userspace.
        ;; CONSOLE_IOCTL_TRY_GET_EVENT is the current case: it returns
        ;; (pressed << 16) | bbkey, and the .iret_cf sign-extend
        ;; would silently zero the press flag at bit 16.
        call fd_ioctl
        jmp .iret_cf_eax

        .io_open:
        ;; SI = filename, AL = flags, DL = mode (when O_CREAT).
        call .check_path
        jc .io_open_bad_pointer
        call fd_open
        jmp .iret_cf
        .io_open_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        .io_read:
        ;; BX = fd, EDI = buffer, ECX = count.  fd_read returns the
        ;; full 32-bit byte count in EAX (or -1 on error), so route
        ;; through the .iret_cf_eax path that skips the sign-extend.
        ;; Bad-buffer is surfaced the same way fd_read surfaces a
        ;; closed-fd error: EAX=-1 + CF=1, no errno encoding.
        push ebx
        mov ebx, edi
        call access_ok
        pop ebx
        jc .io_rw_bad_pointer
        call fd_read
        jmp .iret_cf_eax

        .io_seek:
        ;; BX = fd, ECX = offset (signed 32-bit), AL = whence (0/1/2).
        ;; fd_seek returns EAX = new position (or -1 on error), CF=1 on
        ;; error.  Routed through .iret_cf_eax to preserve the full
        ;; 32-bit position (files can exceed 16 bits — ext2 grows to
        ;; multi-MB easily, and Doom's WAD is several MB).
        call fd_seek
        jmp .iret_cf_eax

        .io_write:
        ;; BX = fd, ESI = buffer, ECX = count.  Same return shape as io_read.
        push ebx
        mov ebx, esi
        call access_ok
        pop ebx
        jc .io_rw_bad_pointer
        call fd_write
        jmp .iret_cf_eax
        .io_rw_bad_pointer:
        or eax, -1
        stc
        jmp .iret_cf_eax

        ;; ------------------------------------------------------------
        ;; Network handlers — bodies in src/syscall/syscalls.c.  Each
        ;; entry is a thin shim that calls the C function and jumps to
        ;; .iret_cf.  sys_net_sendto needs the user's dst_port, which
        ;; lives in the saved EBP slot at [esp+8] (the user passed it
        ;; via EBP because every other register was already taken).
        ;; The shim loads it into EAX before the call so cc.py's
        ;; in_register("ax") sees it as a regular parameter.
        ;; ------------------------------------------------------------

        .net_mac:
        ;; EDI = 6-byte output buffer.
        push ebx
        push ecx
        mov ebx, edi
        mov ecx, 6
        call access_ok
        pop ecx
        pop ebx
        jc .net_bad_pointer
        call sys_net_mac
        jmp .iret_cf

        .net_open:
        call sys_net_open
        jmp .iret_cf

        .net_recvfrom:
        ;; BX = fd, EDI = buffer, ECX = count, DX = port.
        push ebx
        mov ebx, edi
        call access_ok
        pop ebx
        jc .net_bad_pointer
        call sys_net_recvfrom
        jmp .iret_cf

        .net_sendto:
        ;; BX = fd, ESI = payload, ECX = len, EDI = dest IP (4 bytes),
        ;; DX = src port, BP (saved at [esp+8]) = dst port.
        push ebx
        mov ebx, esi
        call access_ok                  ; payload ESI + ECX
        pop ebx
        jc .net_bad_pointer
        push ebx
        push ecx
        mov ebx, edi
        mov ecx, 4
        call access_ok                  ; dest-IP EDI + 4
        pop ecx
        pop ebx
        jc .net_bad_pointer
        mov eax, [esp + 8]              ; saved EBP — low 16 = dst_port
        call sys_net_sendto
        jmp .iret_cf

        .net_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        ;; ------------------------------------------------------------
        ;; Real-time-clock handlers.  Returns that overflow AX (DX:AX
        ;; pairs) get written explicitly into the saved EDX slot so
        ;; the user sees the same value after iretd.
        ;; ------------------------------------------------------------

        .rtc_alarm:
        ;; SYS_RTC_ALARM: arm/disarm the per-process interval timer.
        ;; In:  EBX = ms_until_first_fire (0 = cancel any pending alarm)
        ;;      ECX = ms_interval         (0 = one-shot; non-zero = repeating)
        ;; Out: EAX = ms remaining until the next fire on the previously-
        ;;            armed alarm (0 if no alarm was armed).  CF clear.
        ;; The PIT runs at MS_PER_TICK = 1, so alarm_deadline / interval
        ;; are stored in tick units (which equal ms units on this kernel).
        ;; No error path — any EBX/ECX combination is legal.
        ;; Compute previous remaining ms first (before clobbering state).
        ;; If alarm_deadline is 0, previous is 0.
        ;; Otherwise previous = max(0, alarm_deadline - system_ticks).
        mov edi, [current_program_state]
        mov eax, [edi + PROGRAM_STATE_OFFSET_ALARM_DEADLINE]
        test eax, eax
        jz .rtc_alarm_have_prev
        sub eax, [system_ticks]
        jnc .rtc_alarm_have_prev        ; saved previous in EAX (positive)
        xor eax, eax                    ; deadline already passed → 0
        .rtc_alarm_have_prev:
        mov edx, eax                    ; stash previous in EDX
        ;; Now arm or disarm.
        test ebx, ebx
        jz .rtc_alarm_disarm            ; EBX = 0 → cancel (ECX ignored)
        ;; Arm: alarm_deadline = system_ticks + ebx; alarm_interval = ecx.
        mov eax, [system_ticks]
        add eax, ebx
        mov [edi + PROGRAM_STATE_OFFSET_ALARM_DEADLINE], eax
        mov [edi + PROGRAM_STATE_OFFSET_ALARM_INTERVAL], ecx
        jmp .rtc_alarm_done
        .rtc_alarm_disarm:
        mov dword [edi + PROGRAM_STATE_OFFSET_ALARM_DEADLINE], 0
        mov dword [edi + PROGRAM_STATE_OFFSET_ALARM_INTERVAL], 0
        .rtc_alarm_done:
        mov eax, edx                    ; previous remaining ms -> EAX
        clc
        jmp .iret_cf_eax

        .rtc_datetime:
        ;; Returns EAX = unsigned epoch seconds (UTC), valid through
        ;; 2106-02-07.  CF clear (never errors).
        call rtc_read_epoch
        clc
        jmp .iret_cf_eax

        .rtc_millis:
        ;; Returns EAX = milliseconds since boot.  Wraps at 2^32 ms
        ;; (~49.7 days).  CF clear.
        call rtc_tick_read
        imul eax, MS_PER_TICK
        clc
        jmp .iret_cf_eax

        .rtc_sleep:
        ;; ECX = milliseconds.  rtc_sleep_ms returns CF=0 on completion,
        ;; CF=1 if interrupted by a pending signal.  Propagate as
        ;; ERROR_INTERRUPTED so the libc wrapper can surface EINTR.
        call rtc_sleep_ms
        jc  .rtc_sleep_eintr
        clc
        jmp .iret_cf
        .rtc_sleep_eintr:
        mov al, ERROR_INTERRUPTED
        stc
        jmp .iret_cf

        .rtc_uptime:
        ;; Returns EAX = seconds since boot.  CF clear.  Wraps at 2^32 s
        ;; (~136 years).
        call rtc_tick_read
        xor edx, edx
        mov ecx, TICKS_PER_SECOND
        div ecx
        clc
        jmp .iret_cf_eax

        ;; ------------------------------------------------------------
        ;; Video handlers.
        ;; ------------------------------------------------------------

        ;; SYS_VIDEO_MAP: map the mode-13h framebuffer into the calling
        ;; program's PD at MODE13H_USER_VIRT (RW, U/S=1).  Idempotent —
        ;; mapping an already-mapped slot just overwrites the PTEs with
        ;; the same values.
        ;;
        ;; In:   (none)
        ;; Out:  EAX = MODE13H_USER_VIRT, CF=0 on success.
        ;;       CF=1 with EAX = 0 (NULL) on PT-allocation failure.
        ;;       Setting the FULL 32-bit EAX in the failure path matters
        ;;       because .iret_cf_eax skips the AX→EAX sign-extend that
        ;;       .iret_cf does — a partial `mov ax, 0` would leave the
        ;;       high 16 bits as garbage from the prior register state,
        ;;       so callers that check EAX (rather than CF) would see
        ;;       inconsistent values per call.  NULL is the natural
        ;;       sentinel: success is always the fixed 0xB8000000, never 0.
        ;;
        ;; Uses .iret_cf_eax to preserve the full 32-bit user-virt address.
        ;;
        ;; The mode-13h framebuffer is 64000 bytes ((320*200) — 8 bits per
        ;; pixel, 320x200 indexed-colour).  Fits in 16 pages; we map the
        ;; whole 16 pages (the trailing ~1.5 KB past the actual FB end is
        ;; physical RAM that's part of the same VGA aperture and harmless
        ;; to expose).
        .video_map:
        push esi
        push edi
        push ecx
        push edx
        mov  esi, MODE13H_USER_VIRT     ; ESI walks vaddrs
        mov  edi, MODE13H_PHYS          ; EDI walks paddrs
        mov  ecx, (MODE13H_BYTES + 0xFFF) >> 12   ; ECX = remaining pages
.video_map_loop:
        push ecx
        push edi
        mov  eax, [current_program_state]
        mov  eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov  ebx, esi                   ; vaddr
        mov  ecx, edi                   ; phys
        ;; PTE_USER_RW_SHARED: the AVL[0] PTE_SHARED bit makes
        ;; address_space_destroy skip frame_free on the underlying
        ;; phys pages.  Critical here because MODE13H_PHYS (0xA0000)
        ;; is the VGA aperture, not bitmap-allocator-owned RAM —
        ;; freeing it would inject phantom frames into the free list
        ;; and the next allocation could hand the VGA aperture out
        ;; as user heap, with predictably weird crashes.
        mov  edx, PTE_USER_RW_SHARED
        call address_space_map_page
        pop  edi
        pop  ecx
        jc   .video_map_oom
        add  esi, 0x1000
        add  edi, 0x1000
        dec  ecx
        jnz  .video_map_loop
        mov  eax, MODE13H_USER_VIRT
        clc
        jmp  .video_map_done
.video_map_oom:
        xor  eax, eax                   ; EAX = 0 (NULL) on failure; full 32 bits, not just AX
        stc
.video_map_done:
        pop  edx
        pop  ecx
        pop  edi
        pop  esi
        jmp  .iret_cf_eax

        ;; ------------------------------------------------------------
        ;; Process control handlers.  sys_exec loads the program and
        ;; jmps — never returns through .iret_cf.  sys_exit teleports
        ;; back to the kernel's saved ESP (set by shell_reload /
        ;; sys_exec before each `jmp PROGRAM_BASE`) and re-enters
        ;; shell_reload, which respawns the shell from a clean state.
        ;; ------------------------------------------------------------

        ;; SYS_SYS_BREAK: set/query the program break.  Linux semantics —
        ;; pass 0 to query, an absolute address to set; EAX always holds
        ;; the resulting break (caller compares to requested to detect
        ;; failure).  CF=0 always.
        ;;
        ;; PROGRAM_STATE_OFFSET_PROGRAM_BREAK and
        ;; PROGRAM_STATE_OFFSET_PROGRAM_BREAK_MIN are initialised in
        ;; program_enter (entry.asm) at program load: both start at the
        ;; page-aligned end of the program's loaded image (text + BSS).
        ;;
        ;; Grow-only: requests at or below the current break leave it
        ;; unchanged — userland malloc keeps the freed range in its
        ;; free-list and reuses it.
        ;;
        ;; In:   EBX = new break (0 = query)
        ;; Out:  EAX = resulting break, CF = 0.  Caller compares EAX to
        ;;       requested to detect OOM (returns unchanged old break).
        ;;
        ;; Uses .iret_cf_eax to preserve the full 32-bit EAX (the default
        ;; .iret_cf path sign-extends AX into EAX, which would truncate
        ;; user-space addresses to 16 bits).
        .sys_break:
        push esi
        push edi
        push ebx                                ; [esp] = saved requested
        ;; Query?
        test ebx, ebx
        jz   .sys_break_done
        ;; Below floor?  (Includes the case where caller passes a value
        ;; in the kernel half or otherwise nonsensical.)
        mov  eax, [current_program_state]
        cmp  ebx, [eax + PROGRAM_STATE_OFFSET_PROGRAM_BREAK_MIN]
        jb   .sys_break_done
        ;; Above stack guard?
        cmp  ebx, STACK_VIRT_BASE - 0x10000
        jae  .sys_break_done
        ;; Shrink or no-op?  Returns the unchanged break.
        mov  eax, [current_program_state]
        cmp  ebx, [eax + PROGRAM_STATE_OFFSET_PROGRAM_BREAK]
        jbe  .sys_break_done
        ;; --- Grow loop ---
        ;; ESI walks page-by-page from page_align_up(old_break) to ebx.
        mov  esi, [eax + PROGRAM_STATE_OFFSET_PROGRAM_BREAK]
        add  esi, 0xFFF
        and  esi, 0xFFFFF000
.sys_break_grow:
        cmp  esi, [esp]                         ; reload requested
        jae  .sys_break_commit
        call frame_alloc
        jc   .sys_break_done                      ; OOM — leave break unchanged
        mov  ecx, eax                           ; phys
        mov  ebx, esi                           ; vaddr
        mov  eax, [current_program_state]
        mov  eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov  edx, PTE_USER_RW
        call address_space_map_page
        jc   .sys_break_done                      ; map fail — small frame leak acceptable
        add  esi, 0x1000
        jmp  .sys_break_grow
.sys_break_commit:
        mov  ebx, [esp]                         ; requested
        mov  eax, [current_program_state]
        mov  [eax + PROGRAM_STATE_OFFSET_PROGRAM_BREAK], ebx
.sys_break_done:
        mov  eax, [current_program_state]
        mov  eax, [eax + PROGRAM_STATE_OFFSET_PROGRAM_BREAK]
        pop  ebx                                ; balance the stack (discards saved requested)
        pop  edi
        pop  esi
        clc
        jmp  .iret_cf_eax

        .sys_exec:
        ;; Reject recursive exec from a child — the kernel only tracks
        ;; one suspended parent.
        cmp dword [parent_program_state], 0
        je .sys_exec_no_parent_yet
        mov al, ERROR_INVALID
        stc
        jmp .iret_cf
        .sys_exec_no_parent_yet:
        ;; Snapshot the parent's pushad+iret kernel-stack frame (13 dwords)
        ;; before any internal pushes so [esp .. esp+52) is exactly the
        ;; frame layout child_terminate will restore.  .check_path is a
        ;; net ESP-neutral call (push/pop ECX inside; access_ok_string also
        ;; balances its pushes), so this snapshot is still valid on the
        ;; success path.  If .check_path returns CF=1 the snapshot is
        ;; wasted work but benign — no other path reads parent_iret_frame
        ;; on a failure return.
        mov esi, esp
        mov edi, parent_iret_frame
        mov ecx, 13
        cld
        rep movsd
        ;; ESI = filename in the calling shell's user-virt.  Active PD
        ;; is the shell's; we can read user pages directly until the
        ;; switch-to-template + destroy below.
        ;; Restore ESI (rep movsd advanced it past the frame) before
        ;; .check_path validates the user path pointer.  pushad layout:
        ;; [esp+0]=EDI [esp+4]=ESI so the saved user path is at offset 4.
        mov esi, [esp + 4]
        call .check_path
        jc .exec_bad_pointer
        call vfs_find
        jc .exec_not_found
        test byte [vfs_found_mode], FLAG_EXECUTE
        jnz .exec_load
        mov al, ERROR_NOT_EXECUTE
        stc
        jmp .iret_cf
        .exec_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf
        .exec_not_found:
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
        .exec_load:
        ;; Pre-allocate the new program's USER_DATA handoff frame from
        ;; the bitmap allocator and populate it *directly* from the
        ;; dying shell's user pages.  The frame is reached through
        ;; kmap_map so it stays addressable even when the bitmap
        ;; allocator hands out a frame above the direct-map ceiling
        ;; (FRAME_DIRECT_MAP_LIMIT, ~1020 MB).  The phys is handed off
        ;; to program_enter via [next_handoff_frame_phys].
        ;;
        ;; Reads from BUFFER (user-virt 0x1500) and EXEC_ARG (user-virt
        ;; 0x14FC) below resolve through the shell's PD because we
        ;; haven't switched CR3 yet.  Once the new frame is populated,
        ;; we switch CR3 to kernel_idle_pd; the parent's PD is preserved
        ;; (not destroyed) so child_terminate can restore it later.
        call frame_alloc
        jc .exec_oom
        mov [next_handoff_frame_phys], eax
        push esi
        push edi
        call kmap_map                   ; EAX = handoff kvirt
        push eax                        ; [esp+0] = kvirt; saved for unmap below
        ;; Zero entire frame so unused slots (ARGV etc.) start clean.
        mov edi, eax
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        ;; Reload kvirt (rep stosd advanced edi past the frame).
        mov edi, [esp]
        ;; Copy EXEC_ARG (4 B) and BUFFER (256 B) from the shell PD's
        ;; user pages into the new frame at the matching offsets.
        mov eax, [EXEC_ARG]
        mov [edi + (EXEC_ARG - USER_DATA_BASE)], eax
        mov esi, BUFFER
        add edi, (BUFFER - USER_DATA_BASE)
        mov ecx, MAX_INPUT / 4
        rep movsd
        pop eax                         ; handoff kvirt
        call kmap_unmap
        pop edi
        pop esi
        ;; Do NOT destroy the parent's PD.  Save it, switch slots, switch
        ;; CR3 to kernel_idle_pd, build the child via program_enter.
        mov eax, [current_program_state]
        mov [parent_program_state], eax

        ;; Pick the unused slot for the child.
        cmp eax, program_state_a
        jne .exec_use_slot_a
        mov dword [current_program_state], program_state_b
        jmp .exec_slot_chosen
        .exec_use_slot_a:
        mov dword [current_program_state], program_state_a
        .exec_slot_chosen:

        ;; Zero the child's slot (clears non-fd_table fields: signal
        ;; handlers, pending bits, alarm state, etc.).  fd_table is
        ;; overwritten next with the parent's table — copied here while
        ;; both slots are still addressable.  kernel_stack_top is
        ;; preserved across the wipe — it's a per-slot constant set up
        ;; once by shell_reload and read by tss_set_esp0_for_current_slot
        ;; on every iretd-to-user; the first int 30h from the child
        ;; would #PF at TSS.ESP0=0 if we cleared it here.
        mov edi, [current_program_state]
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP

        ;; Inherit parent's fd_table into child's slot.  Both program_state
        ;; structs live in kernel BSS; straight rep movsd between them.
        mov esi, [parent_program_state]
        add esi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov edi, [current_program_state]
        add edi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov ecx, (FD_MAX * FD_ENTRY_SIZE) / 4
        cld
        rep movsd

        ;; Walk the child's inherited fd_table; for each FD_TYPE_CONSOLE
        ;; entry, zero event_head/event_tail/event_buf so the child doesn't
        ;; inherit keystrokes the parent had buffered before exec.
        mov esi, [current_program_state]
        add esi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov ecx, FD_MAX
.exec_clear_console_ring:
        cmp byte [esi + FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        jne .exec_clear_console_next
        mov byte [esi + FD_OFFSET_EVENT_HEAD], 0
        mov byte [esi + FD_OFFSET_EVENT_TAIL], 0
        push edi
        push ecx
        lea edi, [esi + FD_OFFSET_EVENT_BUF]
        mov ecx, 32 / 4
        xor eax, eax
        cld
        rep stosd
        pop ecx
        pop edi
.exec_clear_console_next:
        add esi, FD_ENTRY_SIZE
        loop .exec_clear_console_ring

        ;; Switch CR3 to kernel_idle_pd; do NOT destroy parent's PD.
        mov eax, [kernel_idle_pd_phys]
        mov cr3, eax
        sti
        jmp program_enter
        .exec_oom:
        ;; frame_alloc returned CF=1 — bitmap exhausted.  Shell stays
        ;; alive (we haven't touched its PD yet); surface the failure
        ;; via ERROR_FAULT so the caller can report and retry / give
        ;; up.  In practice frames are abundant, so this is rare.
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        .sys_exit:
        ;; Encode exit code into the high byte of the wait status; jump to
        ;; child_terminate which destroys the child PD, restores parent
        ;; state, and iretds back into the parent's
        ;; sys_exec syscall return point.  AL = exit code (0..255).
        movzx eax, al
        shl eax, 8
        jmp child_terminate

        .sys_reboot:
        ;; Does not return.
        call reboot

        .sys_shutdown:
        ;; Returns only if the host ignores the shutdown port — surface
        ;; CF=1 so userspace can fall back.
        call shutdown
        stc
        jmp .iret_cf

        ;; SYS_SYS_SIGNAL: register a signal handler.
        ;; In:  EBX = signum (SIGINT or SIGALRM)
        ;;      ECX = handler — SIG_DFL (0), SIG_IGN (1), or user-virt
        ;;            address (PROGRAM_BASE <= ECX < KERNEL_VIRT_BASE).
        ;; Out: EAX = previous handler value, CF clear on success.
        ;;      CF set + AL = ERROR_INVALID on bad signum or out-of-range
        ;;      handler address.
        ;; The previous handler is returned so callers can restore the
        ;; prior state on cleanup, mirroring POSIX signal().
        .sys_signal:
        ;; EBX = signum (SIGINT or SIGALRM); ECX = handler
        ;; (SIG_DFL/SIG_IGN/user-virt).
        cmp ebx, SIGINT
        je  .sys_signal_signum_ok
        cmp ebx, SIGALRM
        jne .sys_signal_bad
        .sys_signal_signum_ok:
        cmp ecx, SIG_IGN
        jbe .sys_signal_handler_ok      ; ECX in {0, 1}
        cmp ecx, PROGRAM_BASE
        jb  .sys_signal_bad
        cmp ecx, KERNEL_VIRT_BASE
        jae .sys_signal_bad
        .sys_signal_handler_ok:
        ;; Route to the right handler slot based on EBX.
        mov edx, [current_program_state]
        cmp ebx, SIGINT
        jne .sys_signal_alarm_slot
        mov eax, [edx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER]   ; previous handler -> EAX
        mov [edx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER], ecx
        jmp .sys_signal_done
        .sys_signal_alarm_slot:
        mov eax, [edx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER]
        mov [edx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER], ecx
        .sys_signal_done:
        clc
        jmp .iret_cf_eax               ; full EAX preserved, CF=0
        .sys_signal_bad:
        mov al, ERROR_INVALID
        stc
        jmp .iret_cf

        ;; SYS_SYS_SIGRETURN: restore the interrupted register state
        ;; from a sigcontext on the user stack and iretd back to user
        ;; code.  signal_resume_after_handler owns the popad and iretd
        ;; — it never returns through .iret_cf — so this entry is a
        ;; bare jmp.  See signal.c for the full sigcontext layout and
        ;; offset arithmetic.
        .sys_sigreturn:
        jmp signal_resume_after_handler

;;; The four net_* C handlers and their `extern` declarations of
;;; fd_alloc / fd_lookup / udp_send / udp_receive / icmp_receive /
;;; ip_send + the ne2k.c file-scope globals (`net_present`,
;;; `mac_address`).  Lives at file scope so the dispatcher's `call
;;; sys_net_*` resolves to global labels.
%include "syscalls.kasm"
