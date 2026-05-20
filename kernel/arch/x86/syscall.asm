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
        SYS_ENTRY SYS_IO_GETDENTS,   .io_getdents
        SYS_ENTRY SYS_IO_IOCTL,      .io_ioctl
        SYS_ENTRY SYS_IO_OPEN,       .io_open
        SYS_ENTRY SYS_IO_READ,       .io_read
        SYS_ENTRY SYS_IO_SEEK,       .io_seek
        SYS_ENTRY SYS_IO_WRITE,      .io_write
        SYS_ENTRY SYS_NET_MAC,       .net_mac
        SYS_ENTRY SYS_NET_OPEN,      .net_open
        SYS_ENTRY SYS_NET_RECVFROM,  .net_recvfrom
        SYS_ENTRY SYS_NET_SENDTO,       .net_sendto
        SYS_ENTRY SYS_NET_SETSOCKOPT,  .net_setsockopt
        SYS_ENTRY SYS_RTC_ALARM,       .rtc_alarm
        SYS_ENTRY SYS_RTC_DATETIME,  .rtc_datetime
        SYS_ENTRY SYS_RTC_MILLIS,    .rtc_millis
        SYS_ENTRY SYS_RTC_SLEEP,     .rtc_sleep
        SYS_ENTRY SYS_RTC_UPTIME,    .rtc_uptime
        SYS_ENTRY SYS_VIDEO_MAP,     .video_map
        SYS_ENTRY SYS_SYS_BREAK,     .sys_break
        SYS_ENTRY SYS_SYS_EXEC,      .sys_exec
        SYS_ENTRY SYS_SYS_EXIT,      .sys_exit
        SYS_ENTRY SYS_SYS_PIPELINE2, .sys_pipeline2
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
        ;; `kernel/syscall/syscalls.c` and the table entries below are
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

        .io_getdents:
        ;; BX = fd, EDI = user buffer, ECX = count.  Validate buffer,
        ;; look up the fd, refuse if not a directory; otherwise stamp
        ;; dir_emit state and dispatch into vfs_read_dir.  Returns
        ;; AX = bytes written (0 at EOF), CF on error.
        push ebx
        mov ebx, edi
        call access_ok
        pop ebx
        jc .io_rw_bad_pointer
        call fd_lookup
        jc .io_rw_bad_pointer                 ; bad fd: EAX=-1 + CF
        cmp byte [esi + FD_OFFSET_TYPE], FD_TYPE_DIRECTORY
        jne .io_getdents_not_dir
        mov [_g_dir_emit_cursor], edi
        mov [_g_dir_emit_remaining], ecx
        push ecx                              ; original count for bytes-written math
        call vfs_read_dir
        pop ecx
        jc .io_rw_bad_pointer                 ; disk error: EAX=-1 + CF
        mov eax, ecx
        sub eax, [_g_dir_emit_remaining]
        clc
        jmp .iret_cf_eax
        .io_getdents_not_dir:
        mov al, ERROR_NOT_DIRECTORY
        stc
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
        ;; Network handlers — bodies in kernel/syscall/syscalls.c.  Each
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

        .net_setsockopt:
        ;; BX = fd, AL = option_name, ECX = value.
        call sys_net_setsockopt
        jmp .iret_cf

        .net_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf

        ;; ------------------------------------------------------------
        ;; Pipeline / exec helpers + SYS_SYS_PIPELINE2 handler.
        ;;
        ;; .validate_user_argv and .build_child_slot factor the
        ;; per-child setup that both .sys_exec and .sys_pipeline2 need:
        ;; access_ok-walk the user-supplied argv (char**) under the
        ;; parent's PD; wipe the child slot; inherit the parent's
        ;; fd_table; and clear console event rings in the copy.  The
        ;; user argv pointer itself is stashed in pending_argv_user_ptr;
        ;; stage_user_argv (inside build_child_program_state) re-walks
        ;; it under the still-active parent PD and writes each string
        ;; directly into the child's stack frame through a kmap alias,
        ;; with no kernel scratch buffer in between.
        ;;
        ;; .sys_pipeline2 builds slot_b with cmd1, slot_c with cmd2,
        ;; installs pipe fds on STDOUT(b) / STDIN(c), and cooperatively
        ;; schedules them via kernel_yield_to_pipeline_start.  Returns
        ;; cmd2's wait status to the shell once both children exit.
        ;; ------------------------------------------------------------

        .build_child_slot:
        ;; Wipe [current_program_state] preserving its kernel_stack_top,
        ;; copy [parent_program_state]'s fd_table into the just-zeroed
        ;; slot, then clear FD_TYPE_CONSOLE event rings in the copy so
        ;; the child doesn't inherit buffered keystrokes from before
        ;; the exec.
        ;;
        ;; In:  [current_program_state] = child slot,
        ;;      [parent_program_state]  = parent slot.
        ;; Clobbers: EAX, ECX, EDX, ESI, EDI.
        mov edi, [current_program_state]
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP
        ;; Copy parent's fd_table into the child slot.
        mov esi, [parent_program_state]
        add esi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov edi, [current_program_state]
        add edi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov ecx, (FD_MAX * FD_ENTRY_SIZE) / 4
        cld
        rep movsd
        ;; Clear console event rings in the copy.
        mov esi, [current_program_state]
        add esi, PROGRAM_STATE_OFFSET_FD_TABLE
        mov ecx, FD_MAX
.bcs_loop:
        cmp byte [esi + FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        jne .bcs_next
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
.bcs_next:
        add esi, FD_ENTRY_SIZE
        loop .bcs_loop
        ret

        .pipeline_unwind_slot_b:
        ;; Tear down slot_b after a partial pipeline build (cmd1 built
        ;; but cmd2 setup failed before slot_c was committed).  Free
        ;; the pipe pool slot, destroy slot_b's PD, free any pending
        ;; handoff frame allocated for slot_c, wipe slot_b, clear
        ;; parent_program_state + pending_pipeline_pipe.
        ;;
        ;; Also reachable from spawn_failed_unwind (entry.asm) when
        ;; pipeline_partial_state == 1 — slot_c's build_child_program_state
        ;; OOMed after slot_b was fully built.
        ;;
        ;; Clobbers: EAX, EBX, ECX, EDX, EDI.
        mov eax, [pending_pipeline_pipe]
        push eax
        call pipe_release_by_index
        add esp, 4
        mov ebx, program_state_b
        mov eax, [ebx + PROGRAM_STATE_OFFSET_PD_PHYS]
        test eax, eax
        jz .punw_no_pd
        push eax
        call address_space_destroy
        add esp, 4
        mov dword [ebx + PROGRAM_STATE_OFFSET_PD_PHYS], 0
.punw_no_pd:
        ;; Wipe slot_b state, clear parent + pending-pipe globals.
        ;; Restoring current_program_state -> program_state_a is critical:
        ;; without it the eventual iret_cf path runs
        ;; tss_set_esp0_for_current_slot on slot_b and the next int 0x30
        ;; from the shell would enter the syscall handler with slot_b's
        ;; (now FD_TYPE_FREE) fd_table — wedging the shell.
        mov edi, program_state_b
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP
        mov dword [parent_program_state], 0
        mov dword [pending_pipeline_pipe], 0
        mov dword [pipeline_active], 0
        mov dword [pipeline_partial_state], 0
        mov dword [current_program_state], program_state_a
        ret

        .validate_user_argv:
        ;; Validate a user-supplied argv (char**) under the caller's
        ;; active PD.  Walks the pointer array up to MAX_ARGV_ENTRIES
        ;; slots (stopping at a NULL terminator); access_ok's the array
        ;; range, then access_ok_string's each element with an ARG_MAX
        ;; byte cap.  No copy — stage_user_argv re-walks under the same
        ;; PD later and reads the bytes directly into the child's stack
        ;; frame via kmap.  Single-threaded kernel + suspended user =
        ;; no TOCTOU between this validation and the later copy.
        ;;
        ;; In:  ESI = user_argv (char**), or 0 (treated as argc=0).
        ;;      Active CR3 = caller's PD.
        ;; Out: CF=0 on success.  CF=1 + AL = ERROR_FAULT (bad pointer
        ;;      or no NUL within ARG_MAX) or ERROR_INVALID (more than
        ;;      MAX_ARGV_ENTRIES entries) on failure.
        ;; Clobbers: EAX, EBX, ECX.  Preserves ESI, EDI, EDX, EBP.
        test esi, esi
        jz .vua_ok

        ;; access_ok on (MAX_ARGV_ENTRIES + 1)*4 bytes (the worst-case
        ;; array footprint).  access_ok preserves all caller registers.
        push esi
        mov ebx, esi
        mov ecx, (MAX_ARGV_ENTRIES + 1) * 4
        call access_ok
        pop esi
        jc .vua_fault

        xor ebx, ebx                            ; ebx = i
.vua_loop:
        cmp ebx, MAX_ARGV_ENTRIES
        jae .vua_too_many
        mov eax, [esi + ebx*4]
        test eax, eax
        jz .vua_ok
        push esi
        push ebx
        mov esi, eax
        mov ecx, ARG_MAX
        call access_ok_string
        pop ebx
        pop esi
        jc .vua_fault
        inc ebx
        jmp .vua_loop

.vua_ok:
        clc
        ret
.vua_fault:
        mov al, ERROR_FAULT
        stc
        ret
.vua_too_many:
        mov al, ERROR_INVALID
        stc
        ret

        .sys_pipeline2:
        ;; SYS_SYS_PIPELINE2: cooperatively run two pipeline children
        ;; (cmd1 | cmd2) in slot_b and slot_c, return when both have
        ;; exited with cmd2's wait status in EAX.
        ;;
        ;; In:  ESI = left_path  (cmd1 path, in shell user-virt)
        ;;      EDI = right_path (cmd2 path, in shell user-virt)
        ;;      EDX = left_argv  (cmd1 argv char**, in shell user-virt; 0 = none)
        ;;      ECX = right_argv (cmd2 argv char**, in shell user-virt; 0 = none)
        ;; Out: EAX = cmd2 wait status (POSIX-shaped, zero-extended).
        ;;      CF = 0 on success.  CF = 1 + AL = ERROR_* on failure
        ;;      (bad pointer, not found, not executable, OOM, nested
        ;;      pipeline rejection).
        ;;
        ;; Reject nested pipelines: a parent must not already be
        ;; suspended.  Same rule as sys_exec.
        cmp dword [parent_program_state], 0
        je .pipeline_no_parent
        mov al, ERROR_INVALID
        stc
        jmp .iret_cf
.pipeline_no_parent:
        ;; Snapshot the shell's pushad+iret kernel-stack frame so any
        ;; future child_terminate-style return path that uses
        ;; parent_iret_frame finds a consistent snapshot.  In practice
        ;; the pipeline-resume path returns through
        ;; kernel_yield_to_pipeline_start's saved ESP — not through
        ;; parent_iret_frame — but writing the snapshot keeps the
        ;; invariant uniform with sys_exec.
        mov esi, esp
        mov edi, parent_iret_frame
        mov ecx, 13
        cld
        rep movsd
        ;; rep movsd clobbered ESI/EDI; reload left_path from pushad slot.
        ;; pushad layout: [esp+0]=EDI, [esp+4]=ESI, [esp+20]=EDX, [esp+24]=ECX;
        ;; cc.py emitted SI=left_path, DI=right_path, DX=left_args,
        ;; CX=right_args per the SYS_SYS_PIPELINE2 ABI.
        mov esi, [esp + 4]              ; left_path
        call .check_path
        jc .pipeline_bad_pointer
        mov esi, [esp + 0]              ; right_path
        call .check_path
        jc .pipeline_bad_pointer

        ;; Validate both argv arrays under the shell's PD.  No copy —
        ;; stage_user_argv re-walks each array later (while still under
        ;; the shell's PD, since we never swap CR3 during the build)
        ;; and writes the strings directly to each child's stack via
        ;; kmap.
        mov esi, [esp + 20]             ; left_argv
        call .validate_user_argv
        jc .pipeline_argv_failed
        mov esi, [esp + 24]             ; right_argv
        call .validate_user_argv
        jc .pipeline_argv_failed

        ;; Allocate the pipe.
        call pipe_alloc
        cmp eax, 0
        jl .pipeline_no_pipe
        mov [pending_pipeline_pipe], eax

        ;; --- Build slot_b (cmd1: writer side) ---
        mov esi, [esp + 4]              ; left_path
        call vfs_find
        jc .pipeline_b_not_found
        test byte [vfs_found_mode], FLAG_EXECUTE
        jz .pipeline_b_not_execute

        ;; Aim pending_argv_user_ptr at left_argv so stage_user_argv
        ;; (inside build_child_program_state below) reads it under the
        ;; shell's PD — which stays the active CR3 throughout.
        mov eax, [esp + 20]             ; left_argv
        mov [pending_argv_user_ptr], eax

        ;; Take parent <- slot_a (shell), current <- slot_b.
        mov eax, [current_program_state]
        mov [parent_program_state], eax
        mov dword [current_program_state], program_state_b

        ;; Wipe + inherit fd_table from parent (shell).
        call .build_child_slot

        ;; Install the pipe-write fd at STDOUT (fd 1) in slot_b.
        mov ebx, [current_program_state]
        add ebx, PROGRAM_STATE_OFFSET_FD_TABLE
        add ebx, STDOUT * FD_ENTRY_SIZE
        mov byte [ebx + FD_OFFSET_TYPE], FD_TYPE_PIPE_W
        mov eax, [pending_pipeline_pipe]
        mov [ebx + FD_OFFSET_START], ax
        ;; Bump the pipe's writer_fd_open refcount.
        ;; pipe_at takes its index argument in EDX (register convention).
        mov edx, eax
        call pipe_at
        inc byte [eax + PIPE_OFFSET_WRITER_FD_OPEN]

        ;; Build slot_b's PD under the shell's PD (active CR3 = shell).
        ;; stage_user_argv inside build_child_program_state reads
        ;; pending_argv_user_ptr through the shell's mappings and writes
        ;; the result through a kmap alias of the child's stack frame
        ;; (kernel half — works under any PD).  OOM in here unwinds via
        ;; build_child_program_state.oom -> spawn_failed_unwind, which
        ;; closes every slot_b fd before wiping the slot — including the
        ;; STDOUT (FD_TYPE_PIPE_W) entry installed above.  fd_close_pipe
        ;; decrements the writer refcount and (since reader_fd_open is
        ;; still 0) calls pipe_release on the pool slot, so no leak.
        ;; tests/test_pipeline_pool_recycle.py regresses this contract.
        call build_child_program_state

        ;; Prime slot_b's kernel stack so kernel_yield's first resume
        ;; lands at userland_entry_stub, which popad+iretds into user
        ;; code at PROGRAM_BASE.
        call build_initial_iret_frame

        ;; Slot_b is now fully built (PD, kernel-stack iret frame,
        ;; pipe-W fd installed, pipe refcount bumped).  Arm the
        ;; pipeline_partial_state hook so spawn_failed_unwind (called
        ;; from build_child_program_state.oom on slot_c) tears down
        ;; slot_b too instead of leaking its PD + pipe pool slot.
        mov dword [pipeline_partial_state], 1

        ;; --- Build slot_c (cmd2: reader side) ---
        ;; CR3 is still the shell's PD — build_child_program_state didn't
        ;; switch it.  No swap dance needed: vfs_find reads right_path
        ;; through the shell's mappings, and stage_user_argv reads
        ;; right_argv the same way during the build below.
        mov esi, [esp + 0]              ; right_path
        call vfs_find
        jc .pipeline_c_not_found
        test byte [vfs_found_mode], FLAG_EXECUTE
        jz .pipeline_c_not_execute

        ;; Aim pending_argv_user_ptr at right_argv for slot_c.
        mov eax, [esp + 24]             ; right_argv
        mov [pending_argv_user_ptr], eax

        ;; current <- slot_c; parent_program_state still = slot_a.
        mov dword [current_program_state], program_state_c

        ;; Wipe + inherit fd_table from parent (shell).
        call .build_child_slot

        ;; Install the pipe-read fd at STDIN (fd 0) in slot_c.
        mov ebx, [current_program_state]
        add ebx, PROGRAM_STATE_OFFSET_FD_TABLE
        add ebx, STDIN * FD_ENTRY_SIZE
        mov byte [ebx + FD_OFFSET_TYPE], FD_TYPE_PIPE_R
        mov eax, [pending_pipeline_pipe]
        mov [ebx + FD_OFFSET_START], ax
        ;; Bump the pipe's reader_fd_open refcount.
        ;; pipe_at takes its index argument in EDX (register convention).
        mov edx, eax
        call pipe_at
        inc byte [eax + PIPE_OFFSET_READER_FD_OPEN]

        ;; CR3 is still the shell's PD; build_child_program_state runs
        ;; under it so stage_user_argv can read right_argv directly.
        call build_child_program_state
        call build_initial_iret_frame

        ;; --- Mark both children runnable ---
        mov byte [program_state_b + PROGRAM_STATE_OFFSET_STATE], STATE_RUNNING
        mov byte [program_state_c + PROGRAM_STATE_OFFSET_STATE], STATE_RUNNING

        ;; Disarm pipeline_partial_state: both slots are now built and
        ;; either child terminating runs the normal child_terminate path
        ;; (which closes the fd_table and frees the PD per-slot), so
        ;; spawn_failed_unwind must NOT call pipeline_unwind_slot_b if it
        ;; ever runs after this point.
        mov dword [pipeline_partial_state], 0

        ;; Arm the pipeline-active flag so child_terminate routes
        ;; sys_exit / signal-kill exits from slot_b / slot_c through
        ;; kernel_yield rather than the parent_iret_frame restore.
        mov dword [pipeline_active], 1

        ;; --- Hand the shell back as current and yield into slot_b ---
        ;; CR3 is already the shell's PD (we never swapped during the
        ;; builds).  kernel_yield_to_pipeline_start saves slot_a's ESP
        ;; and picks slot_b (running cmd1); slot_b's first kernel_yield
        ;; lands at userland_entry_stub which switches CR3 to slot_b's
        ;; PD and iretds into cmd1.
        mov dword [current_program_state], program_state_a
        sti
        call kernel_yield_to_pipeline_start
        cli

        ;; --- Pipeline complete; both children EXITED.  We're on
        ;;     slot_a's kernel stack again, with slot_a's PD active.
        ;; Read cmd2's wait status, tear down pipeline-level state,
        ;; return success to the shell.
        movzx eax, word [program_state_c + PROGRAM_STATE_OFFSET_WAIT_STATUS]
        ;; Wipe slot_b + slot_c (preserving kernel_stack_top) — both
        ;; PDs were freed by child_terminate's address_space_destroy
        ;; on each child's sys_exit; the fd_tables were closed by
        ;; child_terminate's per-fd close loop (which also dropped the
        ;; pipe refcounts and released the pipe pool slot when both
        ;; ends fully closed).  All that's left is to zero the
        ;; per-slot scheduling state.
        push eax                        ; stash cmd2 wait status
        mov edi, program_state_b
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP
        mov edi, program_state_c
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP
        mov dword [parent_program_state], 0
        mov dword [pending_pipeline_pipe], 0
        mov dword [pipeline_active], 0
        pop eax
        sti
        clc
        jmp .iret_cf_eax

;; --- Early error paths (no slot_b state to unwind) ---
.pipeline_bad_pointer:
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf
.pipeline_no_pipe:
        ;; pipe_alloc returned -1 (all 4 slots in use) — surface as
        ;; ERROR_FAULT (OOM-shaped) to match the pattern other resource
        ;; exhaustion paths use.
        mov al, ERROR_FAULT
        stc
        jmp .iret_cf
.pipeline_b_not_found:
        mov eax, [pending_pipeline_pipe]
        push eax
        call pipe_release_by_index
        add esp, 4
        mov dword [pending_pipeline_pipe], 0
        mov dword [pipeline_active], 0
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
.pipeline_b_not_execute:
        mov eax, [pending_pipeline_pipe]
        push eax
        call pipe_release_by_index
        add esp, 4
        mov dword [pending_pipeline_pipe], 0
        mov dword [pipeline_active], 0
        mov al, ERROR_NOT_EXECUTE
        stc
        jmp .iret_cf
.pipeline_argv_failed:
        ;; .copy_user_argv already set CF=1 and AL=ERROR_*.  We're still
        ;; in the early phase — no pipe slot allocated, no parent slot
        ;; suspended, no child slot built — so iret straight back.
        jmp .iret_cf
;; --- Late error paths (slot_b built; unwind via pipeline_unwind_slot_b) ---
.pipeline_c_not_found:
        call .pipeline_unwind_slot_b
        mov al, ERROR_NOT_FOUND
        stc
        jmp .iret_cf
.pipeline_c_not_execute:
        call .pipeline_unwind_slot_b
        mov al, ERROR_NOT_EXECUTE
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
        ;; Validate the user-supplied argv (char**) at saved EDX under
        ;; the shell's PD.  stage_user_argv (called from
        ;; build_child_program_state) walks the same array again to
        ;; copy strings directly into the new program's stack via
        ;; kmap; we stay on the shell's PD throughout so user-virt
        ;; reads keep resolving.
        ;;
        ;; pushad layout: [esp+20] = saved EDX = user_argv (char**).
        mov esi, [esp + SYSCALL_SAVED_EDX]
        call .validate_user_argv
        jc .exec_argv_failed
        mov [pending_argv_user_ptr], esi
        ;; Do NOT destroy the parent's PD.  Save it, switch slots,
        ;; build the child via program_enter.  CR3 stays on the parent
        ;; throughout build_child_program_state; program_enter does the
        ;; final switch to the child PD immediately before iretd.
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

        ;; Build the child slot: wipe (preserving kernel_stack_top),
        ;; copy parent's fd_table, clear console event rings in the
        ;; copy.
        call .build_child_slot

        sti
        jmp program_enter
        .exec_argv_failed:
        ;; .validate_user_argv set CF=1 and AL=ERROR_*.  Shell is still
        ;; the current slot; no state to unwind.
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
        ;; EBX = signum (SIGINT, SIGPIPE, or SIGALRM); ECX = handler
        ;; (SIG_DFL/SIG_IGN/user-virt).
        cmp ebx, SIGINT
        je  .sys_signal_signum_ok
        cmp ebx, SIGPIPE
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
        je  .sys_signal_int_slot
        cmp ebx, SIGPIPE
        je  .sys_signal_pipe_slot
        mov eax, [edx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER]
        mov [edx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER], ecx
        jmp .sys_signal_done
        .sys_signal_int_slot:
        mov eax, [edx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER]   ; previous handler -> EAX
        mov [edx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER], ecx
        jmp .sys_signal_done
        .sys_signal_pipe_slot:
        mov eax, [edx + PROGRAM_STATE_OFFSET_SIGPIPE_HANDLER]
        mov [edx + PROGRAM_STATE_OFFSET_SIGPIPE_HANDLER], ecx
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
