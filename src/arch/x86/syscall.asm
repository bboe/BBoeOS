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
        ;; Syscalls that return DX:AX are expected to recompose the 32-bit
        ;; value via DX in user code.
        movsx eax, ax
        ;; Fall through to .iret_cf_eax — handlers wanting to return a full
        ;; 32-bit value in EAX (currently io_read / io_write, whose byte
        ;; counts can exceed 32767) prepare EAX themselves and ``jmp
        ;; .iret_cf_eax`` to skip the sign-extend.
        .iret_cf_eax:
        jnc .iret_cf_clear
        or dword [esp + SYSCALL_SAVED_EFLAGS], 1
        jmp .iret_cf_write
        .iret_cf_clear:
        and dword [esp + SYSCALL_SAVED_EFLAGS], ~1
        .iret_cf_write:
        mov [esp + SYSCALL_SAVED_EAX], eax
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
        ;; BX = fd, AL = cmd, other regs per (fd_type, cmd).
        call fd_ioctl
        jmp .iret_cf

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

        .rtc_datetime:
        ;; Returns DX:AX = unsigned epoch seconds (UTC), valid through
        ;; 2106-02-07.  CF clear (never errors).
        call rtc_read_epoch
        mov [esp + SYSCALL_SAVED_EDX], dx
        clc
        jmp .iret_cf

        .rtc_millis:
        ;; Returns DX:AX = milliseconds since boot.  Wraps at 2^32 ms
        ;; (~49.7 days).  CF clear.
        call rtc_tick_read
        imul eax, MS_PER_TICK
        mov edx, eax
        shr edx, 16
        mov [esp + SYSCALL_SAVED_EDX], dx
        clc
        jmp .iret_cf

        .rtc_sleep:
        ;; CX = milliseconds.  rtc_sleep_ms preserves all registers; CF clear.
        call rtc_sleep_ms
        clc
        jmp .iret_cf

        .rtc_uptime:
        ;; Returns AX = seconds since boot.  CF clear.
        call rtc_tick_read
        xor edx, edx
        mov ecx, TICKS_PER_SECOND
        div ecx
        clc
        jmp .iret_cf

        ;; ------------------------------------------------------------
        ;; Process control handlers.  sys_exec loads the program and
        ;; jmps — never returns through .iret_cf.  sys_exit teleports
        ;; back to the kernel's saved ESP (set by shell_reload /
        ;; sys_exec before each `jmp PROGRAM_BASE`) and re-enters
        ;; shell_reload, which respawns the shell from a clean state.
        ;; ------------------------------------------------------------

        .sys_exec:
        ;; ESI = filename in the calling shell's user-virt.  Active PD
        ;; is the shell's; we can read user pages directly until the
        ;; switch-to-template + destroy below.
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
        ;; dying shell's user pages.  The bitmap pool sits inside the
        ;; kernel direct map, so the new frame is reachable at
        ;; kernel-virt (phys + 0xC0000000) regardless of which PD is
        ;; active — we don't need any cross-AS staging buffer.  The
        ;; phys is handed off to program_enter via
        ;; [next_handoff_frame_phys].
        ;;
        ;; Reads from BUFFER (user-virt 0x1500) and EXEC_ARG (user-virt
        ;; 0x14FC) below resolve through the shell's PD because we
        ;; haven't switched CR3 yet.  Once the new frame is populated,
        ;; we tear down the shell PD; the new frame survives because
        ;; address_space_destroy only iterates user-half PTEs of the
        ;; PD it's destroying, and this frame isn't mapped there.
        call frame_alloc
        jc .exec_oom
        mov [next_handoff_frame_phys], eax
        push esi
        push edi
        mov edi, eax
        add edi, 0xC0000000             ; kernel-virt of new frame
        ;; Zero entire frame so unused slots (ARGV etc.) start clean.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        ;; Copy EXEC_ARG (4 B) and BUFFER (256 B) from the shell PD's
        ;; user pages into the new frame at the matching offsets.
        mov eax, [EXEC_ARG]
        mov [edi + (EXEC_ARG - USER_DATA_BASE)], eax
        mov esi, BUFFER
        add edi, (BUFFER - USER_DATA_BASE)
        mov ecx, MAX_INPUT / 4
        rep movsd
        pop edi
        pop esi
        mov esp, [shell_esp]
        ;; Switch CR3 to kernel_pd_template, then destroy the dying shell's PD.
        mov eax, cr3
        push eax
        mov eax, [kernel_pd_template_phys]
        mov cr3, eax
        pop eax
        call address_space_destroy
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
        ;; Tear down the dying program's PD, restore kernel ESP, and
        ;; re-enter shell_reload to respawn.
        mov eax, cr3
        push eax
        mov eax, [kernel_pd_template_phys]
        mov cr3, eax
        pop eax
        call address_space_destroy
        mov esp, [shell_esp]
        sti
        jmp shell_reload

        .sys_reboot:
        ;; Does not return.
        call reboot

        .sys_shutdown:
        ;; Returns only if the host ignores the shutdown port — surface
        ;; CF=1 so userspace can fall back.
        call shutdown
        stc
        jmp .iret_cf

;;; The four net_* C handlers and their `extern` declarations of
;;; fd_alloc / fd_lookup / udp_send / udp_receive / icmp_receive /
;;; ip_send + the ne2k.c file-scope globals (`net_present`,
;;; `mac_address`).  Lives at file scope so the dispatcher's `call
;;; sys_net_*` resolves to global labels.
%include "syscalls.kasm"
