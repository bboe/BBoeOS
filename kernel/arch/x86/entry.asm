;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; protected_mode_entry runs once per boot — TSS / IRQ install, libbboeos
;;; shared-frame allocation, driver / VFS / NIC init, banner — then
;;; falls through into shell_reload.  Active PD on entry: `kernel_idle_pd`
;;; (built by `high_entry`, replaces the boot PD which has been freed).
;;;
;;; shell_reload is the re-entry point for SYS_EXIT (after the dying
;;; program's PD has been torn down by sys_exit).  It vfs_finds bin/shell,
;;; clears pending_argv_argc (a fresh shell inherits no args), and `jmp`s
;;; program_enter.
;;;
;;; program_enter builds a fresh per-program PD via address_space_create,
;;; populates the user-visible regions (program text + BSS, libbboeos code
;;; page, user stack), stages the Linux argv/envp/argc frame onto the
;;; top of the new user stack (see stage_user_argv), snapshots the kernel
;;; ESP for sys_exit, switches CR3, and `iretd`s at CPL=3.
;;;
;;; Any CPU exception fired past this point vectors through `idt.asm`'s
;;; `exc_common` and prints `EXCnn` on COM1.  CPL=3 faults — and CPL=0
;;; #PFs whose CR2 lives in the user half (kernel was dereferencing a
;;; user pointer) — tear down the dying program's PD and jump back to
;;; shell_reload, mirroring sys_exit's teardown.  Anything else is a
;;; kernel bug and halts.
;;; ------------------------------------------------------------------------

        PMODE_IRQ0_VECTOR       equ 0x20        ; matches the pic_remap master base
        PMODE_IRQ3_VECTOR       equ 0x23
        PMODE_IRQ4_VECTOR       equ 0x24
        PMODE_IRQ5_VECTOR       equ 0x25
        PMODE_IRQ6_VECTOR       equ 0x26

        ;; User-page PTE flag bundles.
        PTE_USER_RW             equ 0x107       ; P | RW | U
        PTE_USER_RX             equ 0x105       ; P | U (read-only — no NX in 32-bit non-PAE)
        PTE_USER_RX_SHARED      equ (PTE_USER_RX | ADDRESS_SPACE_PTE_SHARED)
        PTE_USER_RW_SHARED      equ (PTE_USER_RW | ADDRESS_SPACE_PTE_SHARED)

        ;; User address-space layout (Linux-shape, PROGRAM_BASE = 0x08048000):
        ;;   PTE 0x00000             : NOT MAPPED — NULL guard (deref → #PF)
        ;;   PTE 0x00010             : shared  — libbboeos code page (R-X)
        ;;   PTEs 0x08048..          : private — program text + BSS
        ;;   PTEs 0xFF7E0..0xFF7EF   : NOT MAPPED — stack guard (overflow → #PF)
        ;;   PTEs 0xFF7F0..0xFF7FF   : private — user stack (16 × 4 KB = 64 KB),
        ;;                             stack top = 0xFF800000 (== kernel boundary)
        ;;
        ;; The stack sits just below the user/kernel split so user
        ;; programs get the full 3 GB of user-virt between PROGRAM_BASE
        ;; and the stack for text + BSS + future heap.  Dense use is
        ;; bounded by the bitmap allocator's free-frame count: the
        ;; kernel zero-fills each user frame through a kmap_map alias
        ;; (memory_management/kmap.asm), so frames below the
        ;; direct-map ceiling fast-path through the direct map and
        ;; frames above it (up to FRAME_PHYSICAL_LIMIT, ~4 GB) reach
        ;; the kernel via a slot in the kmap window.
        STACK_VIRT_BASE         equ STACK_VIRT_END - 0x10000            ; 16 × 4 KB
        STACK_VIRT_END          equ USER_STACK_TOP                      ; one past last page; user/kernel boundary (= KERNEL_VIRT_BASE)
        LIBBBOEOS_VIRT               equ FUNCTION_TABLE                      ; 0x00010000

%include "irq_tail.inc"

pmode_irq0_handler:
        ;; PIT tick.  Increment system_ticks, fire any due interval
        ;; timer (set PENDING_SIGALRM + re-arm or clear) for each live
        ;; program slot, drain due midi events, EOI the master PIC,
        ;; iretd.  Interrupt gate entry leaves IF=0 for the body; on a
        ;; single CPU we don't need LOCK on the inc.  midi_drain_due is
        ;; bounded to MIDI_DRAIN_PER_TICK iterations so the ISR latency
        ;; stays O(1) (the alarm check is constant-time per slot).
        pushad
        inc dword [system_ticks]
        ;; Per-slot alarm check.  Iterate (program_state_a, program_state_b,
        ;; program_state_c); each slot is independently armed.  pd_phys == 0 means the slot
        ;; is unused (no program loaded — e.g., when only the parent is
        ;; live and program_state_b is empty).  Coalescing: if
        ;; PENDING_SIGALRM is already 1 the second set is a no-op
        ;; (handler hasn't run yet; the second fire collapses into the
        ;; first — same model as SIGINT, same as POSIX standard signals).
        push ebx
        mov ebx, program_state_a
        call .pmode_irq0_check_slot
        mov ebx, program_state_b
        call .pmode_irq0_check_slot
        mov ebx, program_state_c
        call .pmode_irq0_check_slot
        pop ebx
        jmp .pmode_irq0_after_alarm

.pmode_irq0_check_slot:
        ;; In: EBX = pointer to program_state slot.
        ;; Out: returns via ret; EAX and ECX clobbered.
        mov eax, [ebx + PROGRAM_STATE_OFFSET_PD_PHYS]
        test eax, eax
        jz .pmode_irq0_check_slot_done
        mov eax, [ebx + PROGRAM_STATE_OFFSET_ALARM_DEADLINE]
        test eax, eax
        jz .pmode_irq0_check_slot_done
        cmp [system_ticks], eax
        jb  .pmode_irq0_check_slot_done
        ;; Fire: set this slot's pending_sigalrm; re-arm or clear deadline.
        mov byte [ebx + PROGRAM_STATE_OFFSET_PENDING_SIGALRM], 1
        mov ecx, [ebx + PROGRAM_STATE_OFFSET_ALARM_INTERVAL]
        test ecx, ecx
        jz .pmode_irq0_slot_oneshot
        ;; Re-arm: deadline = current + interval.  system_ticks wraps at
        ;; 2^32 ms (~49.7 days); an alarm armed near that wrap edge could
        ;; fire at an unexpected time when system_ticks rolls past the
        ;; deadline early.  Not worth fixing for a hobby OS uptime.
        add eax, ecx
        mov [ebx + PROGRAM_STATE_OFFSET_ALARM_DEADLINE], eax
        ret
.pmode_irq0_slot_oneshot:
        mov dword [ebx + PROGRAM_STATE_OFFSET_ALARM_DEADLINE], 0
.pmode_irq0_check_slot_done:
        ret

.pmode_irq0_after_alarm:
        call midi_drain_due
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGNAL_TAIL_CHECK
        iretd

pmode_irq3_handler:
        ;; NE2000 RX (and any other NIC interrupt source — we only
        ;; enable IMR.PRX, so PRX is the only bit that should ever
        ;; be set).  The handler's job is to wake a hlt-parked
        ;; sys_net_recvfrom; the actual ring drain happens later in
        ;; process context via ne2k_receive.  Clear ISR by writing
        ;; 0xFF so the next packet triggers a fresh edge.
        ;;
        ;; ne2k_receive runs only under syscall context with IF=0
        ;; (the INT 30h gate clears it), so this handler never
        ;; preempts a NIC-page switch and the steady-state NIC
        ;; command-register page is 0 — port 0x307 therefore
        ;; resolves to ISR, not the page-1 CURR register.
        pushad
        mov al, 0xFF
        out 0x307, al
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGNAL_TAIL_CHECK
        iretd

pmode_irq4_handler:
        ;; COM1 received-data-ready.  Drain every byte currently in
        ;; the UART RX FIFO into serial_ring via serial_putc — the
        ;; 8259 is in edge-triggered mode, so if we stopped after one
        ;; byte while DR is still asserted, the next byte would never
        ;; generate a fresh edge and would sit in the FIFO until the
        ;; line dropped (i.e. effectively be lost on the test driver's
        ;; line-at-a-time sends).  fd_read_console drains serial_ring
        ;; from process context; the handler's job is to wake a
        ;; hlt-parked reader immediately on keystroke (instead of
        ;; within one PIT tick of polling 0x3FD).
        ;;
        ;; pushad envelope: serial_putc is a cc.py C body that uses
        ;; ECX as scratch; IRQ 4 can fire at any user-mode boundary,
        ;; so anything the C body touches has to be saved.
        pushad
.pmode_irq4_drain:
        ;; 0x3FD / 0x3F8 are > 0xFF, so the immediate `in al, port`
        ;; form truncates the constant; use the DX-indirect form.
        mov dx, 0x3FD                       ; LSR
        in al, dx
        test al, 0x01                       ; DR (data ready)
        jz .pmode_irq4_drained
        mov dx, 0x3F8                       ; DATA
        in al, dx
        call serial_putc
        jmp .pmode_irq4_drain
.pmode_irq4_drained:
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGNAL_TAIL_CHECK
        iretd

pmode_irq5_handler:
        ;; SB16 auto-init block boundary.  Refill the just-finished DMA
        ;; half from the software ring (sb16_refill in drivers/sb16.c)
        ;; while the DSP keeps streaming the other half.  Order:
        ;;   1. Ack the 8-bit IRQ on the card by reading DSP_READ_STATUS.
        ;;      Doing this before the EOI matters — if we EOI PIC1 first
        ;;      while the card is still asserting IRQ 5, the next sti
        ;;      will re-enter immediately.
        ;;   2. Run sb16_refill to drain the ring → just-finished half
        ;;      (sets audio_wakeup so a ring-full producer parked on
        ;;      sti+hlt advances).
        ;;   3. EOI PIC1.
        ;; SB16_DSP_READ_STATUS (0x22E) is > 0xFF, so the immediate-port
        ;; form `in al, port` won't encode (NASM silently truncates the
        ;; 16-bit constant to 8 bits, would land on port 0x2E instead).
        ;; Load the port into DX and use `in al, dx`.
        ;;
        ;; The handler runs with IF=0 (interrupt gate), so sb16_refill
        ;; can read audio_ring_head and update audio_ring_tail without
        ;; bracketing.  pushad/popad covers every C-clobbered GP reg;
        ;; DS/ES are unchanged and both kernel + user data segments
        ;; are flat 4 GB so cc.py's flat addressing works regardless
        ;; of which selector userland left in DS.
        pushad
        mov dx, SB16_DSP_READ_STATUS
        in al, dx
        call sb16_refill
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGNAL_TAIL_CHECK
        iretd

pmode_irq6_handler:
        ;; FDC command complete.  EOI.  pushad/popad (rather than the
        ;; minimal `push eax / pop eax`) so the SIGNAL_TAIL_CHECK macro
        ;; sees a pushad-shape stack and can capture full register state
        ;; into a sigcontext if a user handler is registered.
        pushad
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        SIGNAL_TAIL_CHECK
        iretd

;;; -----------------------------------------------------------------------
;;; program_enter
;;;
;;; Builds the per-program PD and `iretd`s into ring 3.  Caller
;;; invariants:
;;;   * Active PD = the parent's (sys_exec / sys_pipeline2 path) so
;;;     stage_user_argv can dereference user-virt argv directly; or
;;;     `kernel_idle_pd` (boot / shell_reload path, where
;;;     pending_argv_user_ptr is NULL and stage_user_argv touches no
;;;     user pages).
;;;   * `vfs_find` (or equivalent) has populated `vfs_found_*` for
;;;     the binary file.
;;;   * `pending_argv_user_ptr` is either a user-virt `char**` that
;;;     `.validate_user_argv` has already accepted under the active
;;;     PD, or 0 — boot / shell_reload's "no args" case yields an
;;;     `argc=0, argv[0]=NULL, envp[0]=NULL` startup frame.
;;;
;;; Streams the binary directly from disk into per-program user
;;; frames — sector-by-sector via `vfs_read_sec` and a private
;;; `program_fd` struct — instead of staging through a scratch
;;; buffer.  The trailer (BSS size) is read from the last loaded
;;; user frame after the binary stream finishes; BSS-only frames
;;; are then mapped + zero-filled in a follow-up loop.
;;;
;;; Never returns.  On panic (allocator OOM or disk error during
;;; PD build) the kernel halts — there's no graceful recovery for
;;; "ran out of frames / lost a sector mid-program-load" yet.
;;;
;;; The PD-build body is factored into `build_child_program_state`
;;; (below) so sys_pipeline2 can reuse it without the trailing iretd
;;; — its children's first run is reached via kernel_yield, not iretd.
;;; -----------------------------------------------------------------------
program_enter:
        ;; Boot / shell_reload: initialize fresh fd_table.  sys_exec:
        ;; .exec_load already copied parent's fd_table into the child's
        ;; slot via .build_child_slot; skip fd_init so the inheritance
        ;; isn't clobbered.  parent_program_state is the discriminator —
        ;; non-zero means a parent is suspended (sys_exec or sys_pipeline2
        ;; path) and the child slot already has its inherited fd_table.
        cmp dword [parent_program_state], 0
        jne .skip_fd_init
        call fd_init
.skip_fd_init:
        call build_child_program_state
        ;; --- Switch CR3 to the child PD ---
        ;; build_child_program_state ran under the *parent*'s PD (so
        ;; stage_user_argv could read argv directly from user-virt).
        ;; The iretd below needs the child PD active so PROGRAM_BASE
        ;; and the user stack resolve.
        mov eax, [current_program_state]
        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov cr3, eax
        ;; --- iretd into ring 3 ---
        ;; Reload data segments to USER_DATA_SELECTOR before the iretd
        ;; (iretd doesn't reload DS/ES/FS/GS).  CPL=0 can still
        ;; read/write through those selectors because CPL <= DPL on
        ;; access.
        mov ax, USER_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov fs, ax
        mov gs, ax
        push dword USER_DATA_SELECTOR
        mov eax, [current_program_state]
        push dword [eax + PROGRAM_STATE_OFFSET_INITIAL_ESP]
        push dword 0x202
        push dword USER_CODE_SELECTOR
        push dword PROGRAM_BASE
        ;; Update TSS.ESP0 to the entering slot's kernel-stack top so the
        ;; next ring-3-to-ring-0 transition (syscall / IRQ / exception)
        ;; lands on this slot's private kernel stack.
        call tss_set_esp0_for_current_slot
        iretd

;;; -----------------------------------------------------------------------
;;; build_child_program_state
;;;
;;; PD-build core extracted from program_enter.  Allocates the
;;; per-program PD, populates the user-visible regions (handoff
;;; frame, program text + BSS, libbboeos code page, user stack), stages
;;; the Linux argv/envp/argc frame onto the user stack via kmap,
;;; enables the FPU, drains PS/2, and returns.  Does **not** switch
;;; CR3 — the caller stays under the parent's (or kernel_idle_pd's)
;;; address space so stage_user_argv can read the user argv array
;;; directly via the active PD; the iretd / yield path is responsible
;;; for switching to the new PD before user code runs.
;;;
;;; Caller invariants: vfs_found_* populated; pending_argv_user_ptr
;;; either NULL (boot / shell_reload / programs that pass no args) or
;;; a user-virt char** that .validate_user_argv has already accepted;
;;; active CR3 must be the address space where pending_argv_user_ptr
;;; (and its element strings) resolve.
;;;
;;; OOM during the build does NOT return; it falls through to the local
;;; .oom path which either (a) `jmp spawn_failed_unwind` (if a parent
;;; is suspended — sys_exec or sys_pipeline2 failure), (b) prints a
;;; message and `jmp shell_reload` (shell-load-itself OOM is fatal —
;;; gated by loading_shell_flag → .panic).
;;; -----------------------------------------------------------------------
build_child_program_state:
        ;; --- Allocate fresh PD ---
        call address_space_create
        jc .oom
        mov edx, [current_program_state]
        mov [edx + PROGRAM_STATE_OFFSET_PD_PHYS], eax

        ;; --- Set up kernel-side fd struct from vfs_found_* ---
        ;; Used by the binary-stream loop's vfs_read_sec calls to walk
        ;; the binary sector-by-sector without going through fd_alloc
        ;; or the user fd table.  Lives in BSS; only one program loads
        ;; at a time.
        mov edi, program_fd
        xor eax, eax
        mov ecx, FD_ENTRY_SIZE / 4
        cld
        rep stosd
        mov al, [vfs_found_type]
        mov [program_fd + FD_OFFSET_TYPE], al
        mov ax, [vfs_found_inode]
        mov [program_fd + FD_OFFSET_START], ax
        mov eax, [vfs_found_size]
        mov [program_fd + FD_OFFSET_SIZE], eax
        mov ax, [vfs_found_dir_sec]
        mov [program_fd + FD_OFFSET_DIRECTORY_SECTOR], ax
        mov ax, [vfs_found_dir_off]
        mov [program_fd + FD_OFFSET_DIRECTORY_OFFSET], ax

        ;; --- Stream binary pages directly from disk ---
        ;; Each loaded user frame is zero-filled then populated sector-
        ;; by-sector via vfs_read_sec into sector_buffer + a memcpy into
        ;; the frame's direct-map alias.  Last binary frame's phys is
        ;; stashed so the trailer can be peeked after the loop.
        mov dword [last_binary_frame_phys], 0
        mov dword [virt_cursor], PROGRAM_BASE
.binary_page_loop:
        mov eax, [virt_cursor]
        sub eax, PROGRAM_BASE               ; EAX = file byte offset for this page
        cmp eax, [vfs_found_size]
        jae .binary_done                    ; past binary end

        call frame_alloc
        jc .oom
        mov [last_binary_frame_phys], eax   ; remember for trailer peek
        mov [pending_frame_phys], eax       ; track for OOM cleanup
        call kmap_map                       ; EAX = kvirt
        mov edi, eax                        ; EDI = kvirt; held across the sector copy below

        ;; Zero entire frame so the partial last sector lands on a
        ;; zero background.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi

        ;; Inner loop: 8 sectors per page (or fewer at end of file).
        xor edx, edx                        ; sector_in_page index
.binary_sector_loop:
        cmp edx, 8
        jae .binary_page_done

        ;; file_offset = (virt_cursor - PROGRAM_BASE) + sector_in_page * 512
        mov eax, [virt_cursor]
        sub eax, PROGRAM_BASE
        mov ebx, edx
        shl ebx, 9
        add eax, ebx                        ; EAX = file offset for this sector
        cmp eax, [vfs_found_size]
        jae .binary_page_done               ; past end of binary

        ;; bytes_remaining = binsize - file_offset (bytes still to copy)
        mov ebx, [vfs_found_size]
        sub ebx, eax                        ; EBX = remaining
        cmp ebx, 512
        jbe .binary_chunk_set
        mov ebx, 512
.binary_chunk_set:

        mov [program_fd + FD_OFFSET_POSITION], eax

        ;; Read one sector into sector_buffer.
        push ebx
        push edx
        push edi
        mov esi, program_fd
        call vfs_read_sec
        pop edi
        pop edx
        pop ebx
        jc .oom                             ; disk error mid-program-load

        ;; Copy EBX bytes from sector_buffer to (frame + sector_in_page * 512).
        push esi
        push edi
        push edx
        push ecx
        mov esi, [sector_buffer]
        mov ecx, edx
        shl ecx, 9                          ; ECX = sector_in_page * 512
        add edi, ecx                        ; EDI = frame + offset
        mov ecx, ebx
        cld
        rep movsb
        pop ecx
        pop edx
        pop edi
        pop esi

        inc edx
        jmp .binary_sector_loop
.binary_page_done:
        ;; Release the kmap before installing the user-side mapping.
        ;; The frame keeps its data — we just don't need its kernel
        ;; alias anymore.  EDI holds the kvirt from the page setup.
        mov eax, edi
        call kmap_unmap
        ;; Map the frame into the per-program PD at virt_cursor.
        mov ecx, [pending_frame_phys]       ; frame phys
        mov eax, [current_program_state]
        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .oom
        mov dword [pending_frame_phys], 0
        add dword [virt_cursor], 0x1000
        jmp .binary_page_loop
.binary_done:

        ;; --- Read BSS trailer from the last binary frame ---
        ;; binsize is vfs_found_size; the trailer (6-byte BSS_MAGIC32 or
        ;; legacy 4-byte BSS_MAGIC) sits at offset (binsize - N) within
        ;; the file, which lands inside the last loaded frame at offset
        ;; ((binsize - 1) & 0xFFF) + 1 - N.  The frame was kmap_unmap'd
        ;; at the end of the binary stream — re-map it briefly for the peek.
        xor ebx, ebx                        ; default bss_size = 0
        mov eax, [last_binary_frame_phys]
        test eax, eax
        jz .have_bss_size                   ; empty file (no binary loaded)
        call kmap_map                       ; EAX = trailer kvirt
        push eax                            ; save kvirt for the unmap below
        mov ecx, [vfs_found_size]
        sub ecx, 1
        and ecx, 0xFFF
        inc ecx                             ; ECX = valid bytes in last frame
        ;; Try 6-byte trailer first (BSS_MAGIC32).
        cmp ecx, 6
        jb .check_old_trailer
        cmp word [eax + ecx - 2], BSS_MAGIC32
        jne .check_old_trailer
        mov ebx, [eax + ecx - 6]
        jmp .trailer_peek_done
.check_old_trailer:
        cmp ecx, 4
        jb .trailer_peek_done
        cmp word [eax + ecx - 2], BSS_MAGIC
        jne .trailer_peek_done
        movzx ebx, word [eax + ecx - 4]
.trailer_peek_done:
        pop eax                             ; trailer kvirt
        call kmap_unmap
.have_bss_size:

        ;; --- Compute user_image_end ---
        mov eax, [vfs_found_size]
        add eax, ebx                        ; binsize + bsssize
        add eax, PROGRAM_BASE
        add eax, 0xFFF
        and eax, 0xFFFFF000
        mov [user_image_end], eax

        ;; --- Initialise the program break to top of loaded image ---
        ;; PROGRAM_STATE_OFFSET_PROGRAM_BREAK starts at user_image_end
        ;; (page-aligned end of the program's text + BSS).
        ;; PROGRAM_STATE_OFFSET_PROGRAM_BREAK_MIN is the floor — sys_break
        ;; refuses to shrink below it.  Both reset on every program load
        ;; (boot shell, sys_exec, sys_exit reload).
        mov edx, [current_program_state]
        mov [edx + PROGRAM_STATE_OFFSET_PROGRAM_BREAK],     eax
        mov [edx + PROGRAM_STATE_OFFSET_PROGRAM_BREAK_MIN], eax

        ;; Reset signal state — every new program starts in SIG_DFL
        ;; for both signals, with no pending bits, no nesting flag,
        ;; and no armed alarm.  Alarms do not survive exec (POSIX).
        ;; EDX already holds [current_program_state] from above.
        mov dword [edx + PROGRAM_STATE_OFFSET_ALARM_DEADLINE],    0
        mov dword [edx + PROGRAM_STATE_OFFSET_ALARM_INTERVAL],    0
        mov byte  [edx + PROGRAM_STATE_OFFSET_IN_SIGNAL_HANDLER], 0
        mov byte  [edx + PROGRAM_STATE_OFFSET_PENDING_SIGALRM],   0
        mov byte  [edx + PROGRAM_STATE_OFFSET_PENDING_SIGINT],    0
        mov byte  [edx + PROGRAM_STATE_OFFSET_PENDING_SIGPIPE],   0
        mov dword [edx + PROGRAM_STATE_OFFSET_SIGALRM_HANDLER], SIG_DFL
        mov dword [edx + PROGRAM_STATE_OFFSET_SIGINT_HANDLER],  SIG_DFL
        mov dword [edx + PROGRAM_STATE_OFFSET_SIGPIPE_HANDLER], SIG_DFL

        ;; --- BSS-only pages (zero-filled, no disk reads) ---
        ;; virt_cursor was left at page_align_up(PROGRAM_BASE + binsize)
        ;; by the binary stream above; loop until user_image_end.
.bss_page_loop:
        mov eax, [virt_cursor]
        cmp eax, [user_image_end]
        jae .prog_pages_done
        call frame_alloc
        jc .oom
        mov [pending_frame_phys], eax
        call kmap_map                       ; EAX = kvirt
        push eax                            ; save kvirt for the unmap below
        mov edi, eax
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop eax                             ; kvirt
        call kmap_unmap
        mov ecx, [pending_frame_phys]
        mov eax, [current_program_state]
        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .oom
        mov dword [pending_frame_phys], 0
        add dword [virt_cursor], 0x1000
        jmp .bss_page_loop
.prog_pages_done:

        ;; --- Map libbboeos code pages (shared, R-X user) ---
        ;; libbboeos_install populated libbboeos_page_count frames at boot (one per 4 KB
        ;; of libbboeos rounded up).  Alias each one into this PD at
        ;; consecutive user-virts LIBBBOEOS_VIRT + i*0x1000 so the helper page,
        ;; pointer table, and any additional pages all sit contiguously in
        ;; userspace.  PTE_USER_RX_SHARED's AVL[0] bit keeps
        ;; address_space_destroy from freeing the shared frames on exit.
        mov dword [virt_cursor], 0
.libbboeos_map_loop:
        mov eax, [virt_cursor]
        cmp eax, [libbboeos_page_count]
        jae .libbboeos_map_done
        mov ebx, eax
        shl ebx, 2
        mov ecx, [libbboeos_code_phys + ebx]         ; phys of frame i
        mov eax, [virt_cursor]
        shl eax, 12
        add eax, LIBBBOEOS_VIRT
        mov ebx, eax                            ; ebx = LIBBBOEOS_VIRT + i*0x1000
        mov eax, [current_program_state]
        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov edx, PTE_USER_RX_SHARED
        call address_space_map_page
        jc .oom
        inc dword [virt_cursor]
        jmp .libbboeos_map_loop
.libbboeos_map_done:

        ;; --- Map user stack (private, 16 frames, zeroed) ---
        ;; Captures the topmost frame's phys (the one mapped at virt
        ;; STACK_VIRT_END - 0x1000 = 0xFF7FF000) into topmost_stack_frame_phys
        ;; on every iteration; after the loop the last write wins, so the
        ;; variable holds the frame containing USER_STACK_TOP-1.
        ;; stage_user_argv below consumes it to write the Linux argv
        ;; frame into the high end of the page.
        mov dword [virt_cursor], STACK_VIRT_BASE
.stack_page_loop:
        mov eax, [virt_cursor]
        cmp eax, STACK_VIRT_END
        jae .stack_pages_done
        call frame_alloc
        jc .oom
        mov [pending_frame_phys], eax
        mov [topmost_stack_frame_phys], eax
        call kmap_map                       ; EAX = kvirt
        push eax                            ; save kvirt for the unmap below
        mov edi, eax
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop eax                             ; kvirt
        call kmap_unmap
        mov ecx, [pending_frame_phys]
        mov eax, [current_program_state]
        mov eax, [eax + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .oom
        mov dword [pending_frame_phys], 0
        add dword [virt_cursor], 0x1000
        jmp .stack_page_loop
.stack_pages_done:

        ;; --- Stage Linux argv / envp / argc frame on the topmost
        ;;     user stack page and record initial_esp in the slot.
        ;;     Reads pending_argv_user_ptr (a user-virt char**, 0 for
        ;;     the boot / shell_reload "no args" path), dereferences it
        ;;     under the active CR3 = parent's PD, and writes the
        ;;     resulting frame into the child's stack via a kmap alias
        ;;     of the captured topmost_stack_frame_phys.  CR3 is
        ;;     unchanged.
        call stage_user_argv

        ;; --- Enable x87 FPU for ring-3 ---
        ;; CR0.EM=0 (use FPU instructions, don't trap with #NM),
        ;; CR0.MP=1 (track FPU state for FWAIT correctness),
        ;; CR0.NE=1 (native FP error reporting via #MF instead of
        ;; legacy IRQ-13).  Single-tasking — no FXSAVE/FXRSTOR on
        ;; context switch (there are no context switches).  _start
        ;; runs FNINIT to reset FPU state at program entry.
        mov eax, cr0
        and eax, ~(1 << 2)              ; clear EM
        or  eax, (1 << 1) | (1 << 5)    ; set MP, NE
        mov cr0, eax

        ;; The PD is built and live; if anything below this point
        ;; faulted we'd be in a different recovery story.  Clear the
        ;; loading-shell flag so the next program load (post-iretd
        ;; sys_exec) gets graceful OOM recovery again.
        mov dword [loading_shell_flag], 0

        ;; Drain the cooked-ASCII PS/2 ring so the new program doesn't
        ;; inherit bytes the previous program left buffered (programs
        ;; that drain TRY_GET_EVENT but not TRY_GETC — e.g. a fullscreen
        ;; game — would otherwise leave up to KB_BUFFER_SIZE gameplay
        ;; keys stale in ps2_buf when they exit).  The per-fd event
        ;; queues for TRY_GET_EVENT don't need draining here — fd_init
        ;; above already memset the entire fd table to zero, which
        ;; clears head / tail / buffer for every console fd in one shot.
        call ps2_drain

        ;; PD fully built.  Return to caller (program_enter for the
        ;; sys_exec / boot / shell_reload path, sys_pipeline2 for the
        ;; pipeline-child path).
        ret

.oom:
        ;; Allocator OOM (or disk error) during program load.  If we
        ;; were loading the shell itself, halt the kernel — there is
        ;; nothing to fall back to.  Otherwise tear down the partial PD,
        ;; surface a message, and either return ERROR_FAULT to the parent
        ;; (via spawn_failed_unwind when parent_program_state != 0) or
        ;; bring up a fresh shell so the user can recover and retry.
        cmp dword [loading_shell_flag], 0
        jne .panic

        ;; Free the dangling frame from the alloc-then-map pair that
        ;; just failed (set by every frame_alloc; cleared by every
        ;; matching successful map).  Zero means nothing to free.
        mov eax, [pending_frame_phys]
        test eax, eax
        jz .oom_no_pending
        call frame_free
        mov dword [pending_frame_phys], 0
.oom_no_pending:

        ;; Tear down the partial PD.  address_space_destroy walks user
        ;; PDEs only and frees mapped user pages (skipping shared,
        ;; e.g. the libbboeos PTE), then PTs, then the PD frame.  Safe on a
        ;; half-built PD.  CR3 is kernel_idle_pd at this point — the
        ;; caller (boot, sys_exit, sys_exec) switched to it before
        ;; entering program_enter — so we don't need to switch CR3.
        mov ebx, [current_program_state]
        mov eax, [ebx + PROGRAM_STATE_OFFSET_PD_PHYS]
        test eax, eax
        jz .oom_no_pd
        call address_space_destroy
        mov dword [ebx + PROGRAM_STATE_OFFSET_PD_PHYS], 0
.oom_no_pd:

        ;; Reset kernel ESP.  The kernel stack may have transient pushes
        ;; from inner alloc+map pairs; reset to a known top before any
        ;; further work.
        ;; The parent in these unwind paths is always slot_a (the shell), so
        ;; kernel_stack_top (= kernel_stack_a_top) is the right reset target.
        mov esp, kernel_stack_top

        ;; If a parent is suspended, this is a failed child load — return
        ;; ERROR_FAULT to the parent via spawn_failed_unwind.  No console
        ;; print: surfacing the error in EAX is sufficient.  Otherwise
        ;; (boot path / shell load OOM) print the OOM message, then fall
        ;; back to shell_reload or the .panic path — loading_shell_flag
        ;; distinguishes those.
        cmp dword [parent_program_state], 0
        jne spawn_failed_unwind
        ;; No parent: print the OOM message before fallback / panic.
        mov esi, oom_msg
.oom_print:
        mov al, [esi]
        test al, al
        jz .oom_done
        call put_character
        inc esi
        jmp .oom_print
.oom_done:
        cmp dword [loading_shell_flag], 0
        jne .panic
        jmp shell_reload

.panic:
        ;; OOM while loading the shell — print '!' on COM1 and halt.
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

oom_msg db "exec: out of memory", 13, 10, 0

;;; -----------------------------------------------------------------------
;;; build_initial_iret_frame
;;;
;;; Prime [current_program_state]'s kernel stack with a frame that,
;;; when reached via kernel_yield's `popad; ret`, lands at
;;; userland_entry_stub.  The stub then `popad; iretd`s the slot into
;;; user code at PROGRAM_BASE / USER_STACK_TOP.
;;;
;;; Layout (high to low — kernel_yield consumes the bottom block, then
;;; userland_entry_stub consumes the upper popad area, then iretd
;;; consumes the top cross-priv iret frame):
;;;   [top -  4]  ss      = USER_DATA_SELECTOR
;;;   [top -  8]  esp     = USER_STACK_TOP
;;;   [top - 12]  eflags  = 0x202 (IF=1, reserved bit 1 set)
;;;   [top - 16]  cs      = USER_CODE_SELECTOR
;;;   [top - 20]  eip     = PROGRAM_BASE
;;;   [top - 24]  eax     = 0       \
;;;   [top - 28]  ecx     = 0       |
;;;   [top - 32]  edx     = 0       |
;;;   [top - 36]  ebx     = 0       | userland_entry_stub's popad (8)
;;;   [top - 40]  esp_pushad = 0    |
;;;   [top - 44]  ebp     = 0       |
;;;   [top - 48]  esi     = 0       |
;;;   [top - 52]  edi     = 0       /
;;;   [top - 56]  return addr = userland_entry_stub  <- kernel_yield's ret
;;;   [top - 60]  pad_eax = 0       \
;;;   [top - 64]  pad_ecx = 0       |
;;;   [top - 68]  pad_edx = 0       |
;;;   [top - 72]  pad_ebx = 0       | kernel_yield's popad (8 dummies;
;;;   [top - 76]  pad_esp = 0       | values discarded into the GP file
;;;   [top - 80]  pad_ebp = 0       | which userland_entry_stub will
;;;   [top - 84]  pad_esi = 0       | overwrite via its own popad below)
;;;   [top - 88]  pad_edi = 0       /
;;;
;;; saved_esp is set to (top - 88) so kernel_yield's
;;; `mov esp, saved_esp; popad; ret` consumes the bottom pad block and
;;; lands at userland_entry_stub with the upper popad + iret frame ready.
;;;
;;; In:  [current_program_state] = slot to prime.
;;; Clobbers: EAX, EDX.
;;; -----------------------------------------------------------------------
build_initial_iret_frame:
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_KERNEL_STACK_TOP]
        mov dword [eax - 4],  USER_DATA_SELECTOR
        mov ebx, [edx + PROGRAM_STATE_OFFSET_INITIAL_ESP]
        mov [eax - 8],  ebx
        mov dword [eax - 12], 0x202
        mov dword [eax - 16], USER_CODE_SELECTOR
        mov dword [eax - 20], PROGRAM_BASE
        xor edx, edx
        mov [eax - 24], edx
        mov [eax - 28], edx
        mov [eax - 32], edx
        mov [eax - 36], edx
        mov [eax - 40], edx
        mov [eax - 44], edx
        mov [eax - 48], edx
        mov [eax - 52], edx
        mov dword [eax - 56], userland_entry_stub
        ;; Bottom pad block — kernel_yield's popad consumes these and
        ;; discards the values (userland_entry_stub's own popad fills
        ;; the GP file with the iret-prep zeros above before iretd).
        mov [eax - 60], edx
        mov [eax - 64], edx
        mov [eax - 68], edx
        mov [eax - 72], edx
        mov [eax - 76], edx
        mov [eax - 80], edx
        mov [eax - 84], edx
        mov [eax - 88], edx
        sub eax, 88
        mov edx, [current_program_state]
        mov [edx + PROGRAM_STATE_OFFSET_SAVED_ESP], eax
        ret

protected_mode_entry:
        ;; Segment registers, ESP, GDTR, and IDTR are already in place
        ;; — `high_entry` (kernel.asm) ran first and handed off here
        ;; with the kernel GDT / IDT live and ESP pointing at
        ;; `kernel_stack_top`.  We patch the TSS, ltr, bring up devices,
        ;; allocate the shared libbboeos user-page frame, and drop into
        ;; shell_reload.
        ;;
        ;; Patch the TSS descriptor's base bytes with tss_data's linear
        ;; address (the bytes are scattered across descriptor offsets
        ;; +2/+4/+7 so we can't fold them at assemble time without
        ;; line-noise expressions), populate the TSS fields the CPU
        ;; consults on a ring-3 → ring-0 transition (SS0, ESP0), parking
        ;; the I/O permission bitmap past the TSS limit so all I/O ports
        ;; trap from CPL=3.  Then `ltr` — must complete before any ring
        ;; transition can fire, but exceptions and IRQs at CPL=0 don't
        ;; need the TSS, so doing it before the rest of init is safe.
        mov eax, tss_data
        mov [gdt_tss + 2], ax
        shr eax, 16
        mov [gdt_tss + 4], al
        mov [gdt_tss + 7], ah
        mov dword [tss_data + 4], kernel_stack_top      ; ESP0
        mov word [tss_data + 8], 0x10                   ; SS0 = kernel data
        mov word [tss_data + 102], 104                  ; IOPB offset = TSS limit + 1 → no I/O bitmap
        mov ax, TSS_SELECTOR
        ltr ax

        ;; Reprogram PIT to 1000 Hz (MS_PER_TICK=1 ms/tick).
        mov al, PIT_MODE2_LOHI_CH0
        out PIT_COMMAND, al
        mov al, PIT_DIVISOR & 0xFF
        out PIT_CHANNEL0, al
        mov al, PIT_DIVISOR >> 8
        out PIT_CHANNEL0, al

        ;; Install 32-bit IRQ handlers.
        mov eax, pmode_irq0_handler
        mov bl, PMODE_IRQ0_VECTOR
        call idt_set_gate32
        mov eax, pmode_irq5_handler
        mov bl, PMODE_IRQ5_VECTOR
        call idt_set_gate32
        mov eax, pmode_irq6_handler
        mov bl, PMODE_IRQ6_VECTOR
        call idt_set_gate32

        ;; Zero the system tick counter and unmask IRQ 0 (PIT) before
        ;; the driver inits run, so any timing primitive that runs
        ;; during init (e.g. fdc_motor_start's rtc_sleep_ms during
        ;; vfs_init's first read on a floppy boot) sees ticks
        ;; advancing.
        mov dword [system_ticks], 0
        in al, PIC1_DATA_PORT
        and al, 0FEh                    ; clear bit 0 (unmask IRQ 0)
        out PIC1_DATA_PORT, al
        sti

        call ata_init
        call fd_init
        call fdc_init
        call ps2_init
        call sb16_init
        call serial_init
        call vfs_init

        ;; Load `lib/libbboeos` into the shared user-page frame.
        ;; Runs after vfs_init so vfs_find / vfs_read_sec resolve;
        ;; program_enter maps the resulting frame (with PTE_SHARED)
        ;; into every per-program PD; address_space_destroy skips it
        ;; on teardown.
        call libbboeos_install
        ;; Probe the NE2000 NIC and bring it up if present.  CF set =
        ;; no NIC, which is fine — net programs surface that via a
        ;; "no NIC" message rather than halting the kernel.
        call network_initialize
        jc .skip_ne2k_irq
        ;; NIC came up: install pmode_irq3_handler at vector 0x23 and
        ;; unmask IRQ 3 on PIC1 so an incoming packet wakes a
        ;; hlt-parked sys_net_recvfrom immediately (instead of within
        ;; one PIT tick).  ne2k_init has already enabled IMR.PRX so
        ;; the NIC will assert the line on a received packet.
        mov eax, pmode_irq3_handler
        mov bl, PMODE_IRQ3_VECTOR
        call idt_set_gate32
        in al, PIC1_DATA_PORT
        and al, 0F7h                    ; clear bit 3 (unmask IRQ 3)
        out PIC1_DATA_PORT, al
.skip_ne2k_irq:

        call vga_clear_screen

        ;; Print welcome banner to COM1 and VGA.
        mov esi, welcome_msg
        .banner:
        mov al, [esi]
        test al, al
        jz .banner_done
        call put_character
        inc esi
        jmp .banner
        .banner_done:
        ;; Fall through into shell_reload.

shell_reload:
        ;; Boot path / shell-itself-died fallback — re-establish the
        ;; canonical "shell is the sole live program" state.
        mov dword [parent_program_state], 0
        ;; Clear pipeline globals in case the shell died mid-pipeline
        ;; (e.g. SIGINT killed the shell while sys_pipeline2 was on
        ;; the kernel stack).  Fresh boot path: already zero by BSS.
        mov dword [pipeline_active], 0
        mov dword [pending_pipeline_pipe], 0
        mov edi, parent_iret_frame
        mov ecx, 13
        xor eax, eax
        cld
        rep stosd
        mov edi, program_state_a
        mov ecx, PROGRAM_STATE_SIZE / 4
        xor eax, eax
        rep stosd
        mov edi, program_state_b
        mov ecx, PROGRAM_STATE_SIZE / 4
        xor eax, eax
        rep stosd
        mov edi, program_state_c
        mov ecx, PROGRAM_STATE_SIZE / 4
        xor eax, eax
        rep stosd
        ;; Re-establish each slot's kernel_stack_top after the BSS-style
        ;; wipe above.  tss_set_esp0_for_current_slot reads this field on
        ;; every iretd-to-userland so the next ring-3-to-ring-0 transition
        ;; lands on the running slot's kernel stack.  Slot_a reuses the
        ;; shell's existing kernel stack (kernel_stack_a_top aliases
        ;; kernel_stack_top in kernel.asm); slot_b/c have their own 4 KB
        ;; regions reserved alongside program_state_b/c below.
        mov dword [program_state_a + PROGRAM_STATE_OFFSET_KERNEL_STACK_TOP], kernel_stack_a_top
        mov dword [program_state_b + PROGRAM_STATE_OFFSET_KERNEL_STACK_TOP], kernel_stack_b_top
        mov dword [program_state_c + PROGRAM_STATE_OFFSET_KERNEL_STACK_TOP], kernel_stack_c_top
        mov dword [current_program_state], program_state_a
        ;; Restore 80x25 text mode if a dying program left the VGA card
        ;; in a graphics mode (e.g. Doom in mode 13h).  No-op on the
        ;; first-boot fall-through (vga_current_mode starts at 0x03), so
        ;; the welcome banner above stays on screen.
        call vga_reset_text_mode
        ;; Active PD: kernel_idle_pd (sys_exit just destroyed the
        ;; dying program's PD and switched CR3 off it, or this is the
        ;; first boot and CR3 was set up by high_entry).  Look up bin/shell
        ;; (program_enter streams its bytes from disk on demand) and
        ;; jmp program_enter.  pending_argv_user_ptr = 0 here gives the
        ;; shell an argc=0 startup frame — stage_user_argv writes just
        ;; argc / argv NULL / envp NULL.
        ;;
        ;; loading_shell_flag = 1 promotes any OOM in this load to a
        ;; hard panic (.oom → .panic in program_enter); program_enter
        ;; clears it back to 0 immediately before iretd so the
        ;; subsequent sys_exec from the running shell gets graceful
        ;; recovery again.
        mov dword [loading_shell_flag], 1
        mov esi, shell_path
        call vfs_find
        jc .shell_fail
        mov dword [pending_argv_user_ptr], 0
        jmp program_enter

        .shell_fail:
        ;; Missing or unreadable shell.  Halt — no recovery here.
        cli
        hlt
        jmp $-1

;;; -----------------------------------------------------------------------
;;; child_terminate — single shared exit point for "child went away,
;;; return wait status to parent."  Called from sys_exit, signal_dispatch_kill,
;;; and exc_common (after they've each done their kill-specific prelude).
;;;
;;; In:  EAX = wait-status word (POSIX-shaped, 16-bit).  All other regs free.
;;; Out: never returns; iretds back into the parent's user code at the
;;;      instruction after `int 30h`, with the parent's saved registers
;;;      restored from parent_iret_frame and EAX overwritten with the wait
;;;      status.
;;;
;;; If parent_program_state is null (the shell itself died — only happens
;;; if it removed its SIG_IGN, or a CPU exception fires inside the shell)
;;; this falls back to shell_reload to respawn the shell from disk.
;;; -----------------------------------------------------------------------
child_terminate:
        cli
        ;; If no parent → fall back to shell_reload (shell-itself-died).
        cmp dword [parent_program_state], 0
        je shell_reload

        ;; Stash wait status — register pressure during the destroy/restore
        ;; below would otherwise clobber it.  Use EBX (callee-saved by our
        ;; convention; nothing here calls into C with EBX live).
        mov ebx, eax

        ;; Switch to kernel_idle_pd so address_space_destroy can free the
        ;; child's PD frame (mirrors signal_dispatch_kill / .sys_exit).
        mov eax, [kernel_idle_pd_phys]
        mov cr3, eax

        ;; Destroy the child's PD.
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        push eax
        call address_space_destroy
        add esp, 4
        mov edx, [current_program_state]
        mov dword [edx + PROGRAM_STATE_OFFSET_PD_PHYS], 0

        ;; Close every non-free fd in the child's fd_table before zeroing
        ;; the slot.  This drives the per-type teardown — vfs_update_size
        ;; flush for writable file fds, sb16_close, midi reset — that the
        ;; child would have run if it had called close() itself.  For
        ;; pipeline children, closing the pipe-end fds drives
        ;; fd_close_pipe → pipe_decrement_*; the matching pipe_wake_*
        ;; rewakes a peer parked on the empty/full buffer so it can see
        ;; EOF / EPIPE on its next attempt.
        ;; fd_close needs current_program_state pointing at the child;
        ;; we haven't swapped to the parent yet (see the swap below), so
        ;; that's still correct here.
        ;; EBX currently holds the stashed wait status; save it so the loop
        ;; can use EBX as the fd counter, then restore it afterwards.
        push ebx                        ; stash wait status across close loop
        mov ebx, 0
.child_term_close_loop:
        cmp ebx, FD_MAX
        jge .child_term_close_done
        push ebx
        call fd_close                   ; BX = fd; no return-value use
        pop ebx
        inc ebx
        jmp .child_term_close_loop
.child_term_close_done:
        pop ebx                         ; restore wait status

        ;; Pipeline-child branch.  pipeline_active is non-zero while
        ;; sys_pipeline2 is suspended on
        ;; kernel_yield_to_pipeline_start, so a sys_exit from slot_b /
        ;; slot_c during that window means "pipeline child exiting."
        ;; Mark this slot EXITED, stash wait_status in the slot, and
        ;; kernel_yield — the scheduler will pick the peer if it's
        ;; still RUNNING, or fall back to slot_a (resuming
        ;; sys_pipeline2's epilogue) once both children have exited.
        ;;
        ;; The PD is already destroyed and the fd_table is already
        ;; closed above, so all that's left for this slot is the
        ;; scheduling-state bookkeeping kernel_yield does.  We
        ;; intentionally skip the WIPE_SLOT here so wait_status
        ;; survives until sys_pipeline2 reads it.
        cmp dword [pipeline_active], 0
        je .child_term_non_pipeline
        mov edx, [current_program_state]
        mov [edx + PROGRAM_STATE_OFFSET_WAIT_STATUS], ebx
        mov al, STATE_EXITED
        xor ebx, ebx                    ; not parked on a pipe
        jmp kernel_yield
.child_term_non_pipeline:

        ;; Zero the child's slot (clears fd_table, pending bits, etc.).
        ;; kernel_stack_top is a per-slot constant — preserve it across
        ;; the wipe so the next sys_exec into this slot has the right
        ;; TSS.ESP0 target.
        mov edi, [current_program_state]
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP

        ;; Swap back to parent.
        mov eax, [parent_program_state]
        mov [current_program_state], eax
        mov dword [parent_program_state], 0

        ;; Switch CR3 to parent PD.
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov cr3, eax

        ;; Restore parent kernel-stack frame.  parent_iret_frame holds 13
        ;; dwords (8 pushad slots + 5 cross-priv iret slots).  Place them
        ;; at kernel_stack_top - 52 so popad+iretd consume exactly that.
        ;; The parent in these unwind paths is always slot_a (the shell), so
        ;; kernel_stack_top (= kernel_stack_a_top) is the right reset target.
        mov esp, kernel_stack_top
        sub esp, 52
        mov edi, esp
        mov esi, parent_iret_frame
        mov ecx, 13
        cld
        rep movsd

        ;; Poke wait status into the saved-EAX slot in pushad area.
        ;; pushad layout: [esp + 0] EDI, [esp + 4] ESI, [esp + 8] EBP,
        ;; [esp + 12] ESP_pushad, [esp + 16] EBX, [esp + 20] EDX,
        ;; [esp + 24] ECX, [esp + 28] EAX.
        mov [esp + 28], ebx

        ;; Clear CF in saved EFLAGS.  Saved EFLAGS lives at [esp + 40]
        ;; (cross-priv iret frame: EIP, CS, EFLAGS, ESP, SS at offsets
        ;; 32..48).
        and dword [esp + 40], ~1

        ;; Deliver any signal that fired in the parent's slot during the
        ;; child's run.  SIGNAL_TAIL_CHECK reads pending bits + handlers
        ;; from current_program_state (now the parent), dispatches if
        ;; pending — same path as the IRQ-tail check.
        SIGNAL_TAIL_CHECK
        ;; Update TSS.ESP0 to the parent slot's kernel-stack top so the
        ;; parent's next ring transition lands on its own kernel stack.
        call tss_set_esp0_for_current_slot
        iretd

;;; -----------------------------------------------------------------------
;;; spawn_failed_unwind — sys_exec succeeded vfs_find but the child PD
;;; build failed (OOM in program_enter, disk read error mid-load).  The
;;; child never iretd'd into ring 3, so there is no "wait status"
;;; semantic; the parent's exec() syscall must return with
;;; CF=1, AL=ERROR_FAULT.
;;;
;;; Reach path: program_enter's .oom branch (entry.asm) replaces
;;; "jmp shell_reload" with "jmp spawn_failed_unwind" when
;;; parent_program_state != 0 (set up in Task B7).
;;; -----------------------------------------------------------------------
spawn_failed_unwind:
        cli
        ;; Tear down the partial child PD if any.  The child's pd_phys may
        ;; be non-zero (PD was allocated) or zero (allocator failed
        ;; before address_space_create returned) depending on how far
        ;; program_enter got.
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        test eax, eax
        jz .spawn_failed_no_pd
        push eax
        call address_space_destroy
        add esp, 4
        .spawn_failed_no_pd:

        ;; Close every non-free fd in the child's fd_table before zeroing
        ;; the slot.  sys_exec inherits the parent's fd_table into the
        ;; child slot before program_enter runs (syscall.asm .exec_load);
        ;; if program_enter then fails (OOM during PD build, disk error
        ;; mid-load), those inherited fds need the same per-type teardown
        ;; child_terminate runs — file size flush, sb16_close, midi reset.
        ;; current_program_state still points at the child here.
        mov ebx, 0
.spawn_failed_close_loop:
        cmp ebx, FD_MAX
        jge .spawn_failed_close_done
        push ebx
        call fd_close
        pop ebx
        inc ebx
        jmp .spawn_failed_close_loop
.spawn_failed_close_done:

        ;; Zero the child's slot.  Preserve kernel_stack_top across the
        ;; wipe — it's a per-slot constant the next sys_exec into this
        ;; slot will need (tss_set_esp0_for_current_slot reads it on
        ;; every iretd-to-user).
        mov edi, [current_program_state]
        WIPE_SLOT_PRESERVING_KERNEL_STACK_TOP

        ;; Swap back to parent.
        mov eax, [parent_program_state]
        mov [current_program_state], eax
        mov dword [parent_program_state], 0

        ;; Switch CR3 to parent PD.
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov cr3, eax

        ;; Pipeline-aware tail: if sys_pipeline2 had already finished
        ;; building slot_b when slot_c's build_child_program_state OOMed,
        ;; the unwind above only tore down slot_c.  Slot_b's PD, its
        ;; FD_TYPE_PIPE_W fd, and the pipe pool slot would leak.
        ;; pipeline_partial_state == 1 means "slot_b is built and needs
        ;; cleanup" — call into syscall_handler.pipeline_unwind_slot_b,
        ;; which destroys slot_b's PD, releases the pipe pool slot, and
        ;; clears pipeline_active + pending_pipeline_pipe +
        ;; pipeline_partial_state.  It also re-asserts
        ;; current_program_state = program_state_a (already true here
        ;; from the swap above, idempotent).
        cmp dword [pipeline_partial_state], 0
        je .spawn_failed_no_pipeline
        call syscall_handler.pipeline_unwind_slot_b
.spawn_failed_no_pipeline:

        ;; Restore parent kernel-stack frame, set EAX=ERROR_FAULT, set CF.
        ;; The parent in these unwind paths is always slot_a (the shell), so
        ;; kernel_stack_top (= kernel_stack_a_top) is the right reset target.
        mov esp, kernel_stack_top
        sub esp, 52
        mov edi, esp
        mov esi, parent_iret_frame
        mov ecx, 13
        cld
        rep movsd
        mov dword [esp + 28], ERROR_FAULT
        or dword [esp + 40], 1

        SIGNAL_TAIL_CHECK
        ;; Update TSS.ESP0 to the parent slot's kernel-stack top before
        ;; resuming the parent at CPL=3 — its next ring transition must
        ;; land on its own kernel stack.
        call tss_set_esp0_for_current_slot
        iretd

;;; -----------------------------------------------------------------------
;;; kernel_yield — cooperative slot switch on pipe block (or pipeline-
;;; child exit).  Save the current slot's kernel ESP to its program_state
;;; slot, walk slot_b/slot_c for the next runnable slot, switch CR3, load
;;; the peer's saved ESP, return.  The "return" lands at the peer's last
;;; kernel_yield call site (which is now resuming).
;;;
;;; For a never-run slot (sys_pipeline2 in Task 6 primes its kernel
;;; stack so the first ret target is userland_entry_stub), the peer's
;;; first resume falls through to popad+iretd into userland at
;;; PROGRAM_BASE.
;;;
;;; Precondition: sys_pipeline2 must set slot_b.state and slot_c.state
;;; to STATE_RUNNING before the first kernel_yield call.  A never-
;;; initialized slot has state == STATE_BLOCKED_READ (0) from BSS
;;; zero-init, which the deadlock check below would misread as "not
;;; runnable" — and if BOTH children are still in that state, the
;;; "both EXITED" check fails too, triggering deadlock_panic.
;;;
;;; In:  AL = STATE_BLOCKED_READ | STATE_BLOCKED_WRITE | STATE_EXITED
;;;      EBX = struct pipe* the caller is blocked on (or 0 for EXITED)
;;; Out: returns to whichever slot the scheduler picks.  Caller's
;;;      kernel call chain is preserved on its own slot's kernel stack
;;;      and will resume when the slot is re-scheduled.
;;;
;;; Clobbers: EAX, ECX, EDX (EBX consumed as input).  EBP, ESI, EDI
;;; preserved by virtue of not being touched.
;;;
;;; If both slot_b and slot_c are STATE_EXITED, control returns to
;;; slot_a (the shell) via slot_a.saved_esp — which holds the ESP
;;; that sys_pipeline2 saved just before its first kernel_yield call.
;;; -----------------------------------------------------------------------
;;; kernel_yield_read / kernel_yield_write — cdecl wrappers around
;;; kernel_yield.  Translate the C (struct pipe *p) argument into the
;;; AL/EBX register convention kernel_yield consumes.  Never return to
;;; the C caller; the scheduler resumes whichever slot it picks.
;;; -----------------------------------------------------------------------
global kernel_yield_read
kernel_yield_read:
        mov ebx, [esp + 4]              ; struct pipe *p
        mov al, STATE_BLOCKED_READ
        jmp kernel_yield

global kernel_yield_write
kernel_yield_write:
        mov ebx, [esp + 4]              ; struct pipe *p
        mov al, STATE_BLOCKED_WRITE
        jmp kernel_yield

kernel_yield:
        ;; Save current slot's state.  The parking slot's CPU register
        ;; state needs to survive across the switch — without this,
        ;; callers would have to manually push/pop every callee-saved
        ;; register before invoking kernel_yield.  The original
        ;; kernel_yield_read/write wrappers used to push EBP explicitly
        ;; because syscall_handler's dispatch (`mov ebp, [.table + eax*4];
        ;; jmp ebp`) leaves EBP holding a kernel code address — a peer
        ;; slot's syscall_handler running during the yield clobbered the
        ;; parking slot's EBP, and fd_write_pipe's `mov esp, ebp;
        ;; pop ebp; ret` epilogue would then ret-pop garbage.  pushad here
        ;; covers EBP and the rest of the GP file in one place so
        ;; child_terminate's `jmp kernel_yield` and any future caller
        ;; gets the same guarantee.
        cli
        pushad
        mov edx, [current_program_state]
        mov byte [edx + PROGRAM_STATE_OFFSET_STATE], al
        mov [edx + PROGRAM_STATE_OFFSET_CURRENT_PIPE], ebx
        ;; Park caller on the pipe (only if blocking, not if exiting).
        cmp al, STATE_EXITED
        je .park_done
        cmp al, STATE_BLOCKED_READ
        jne .park_writer
        mov [ebx + PIPE_OFFSET_BLOCKED_READER], edx
        jmp .park_done
.park_writer:
        mov [ebx + PIPE_OFFSET_BLOCKED_WRITER], edx
.park_done:
        ;; Save current ESP (pointing into the just-pushad'd register
        ;; block) into current slot's saved_esp.
        mov [edx + PROGRAM_STATE_OFFSET_SAVED_ESP], esp

        ;; Pick next runnable slot from {slot_b, slot_c} first; only
        ;; fall back to slot_a if both pipeline children have exited.
        mov edx, program_state_b
        cmp byte [edx + PROGRAM_STATE_OFFSET_STATE], STATE_RUNNING
        je .resume_slot
        mov edx, program_state_c
        cmp byte [edx + PROGRAM_STATE_OFFSET_STATE], STATE_RUNNING
        je .resume_slot
        ;; No runnable child.  Both must be EXITED for shell-resume to
        ;; be valid (deadlock case is the only other possibility, and
        ;; with 2 children + 1 pipe it's unreachable).
        mov eax, program_state_b
        cmp byte [eax + PROGRAM_STATE_OFFSET_STATE], STATE_EXITED
        jne .deadlock_panic
        mov eax, program_state_c
        cmp byte [eax + PROGRAM_STATE_OFFSET_STATE], STATE_EXITED
        jne .deadlock_panic
        ;; Pipeline complete — return to shell (slot_a).
        mov edx, program_state_a
.resume_slot:
        ;; EDX = chosen slot.  Switch CR3 + ESP, update TSS.
        mov [current_program_state], edx
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov cr3, eax
        mov esp, [edx + PROGRAM_STATE_OFFSET_SAVED_ESP]
        call tss_set_esp0_for_current_slot
        sti
        popad                  ; restore the resuming slot's GP regs
        ret    ; returns to the saved kernel-mode IP in this slot's stack
.deadlock_panic:
        mov dx, COM1_DATA
        mov al, '*'
        out dx, al
        cli
        hlt
        jmp $-1

;;; -----------------------------------------------------------------------
;;; kernel_yield_to_pipeline_start — sys_pipeline2's first-yield variant
;;; of kernel_yield.  Used exactly once per pipeline: after both children
;;; have been built and marked STATE_RUNNING, the shell calls this to
;;; save slot_a's kernel ESP and switch into the first runnable child.
;;;
;;; Differences from kernel_yield:
;;;   * Does NOT park slot_a on a pipe (the shell isn't blocked on one
;;;     — it's just suspended until both children exit).
;;;   * Does NOT touch slot_a.state — the scheduler distinguishes
;;;     "shell-suspended" from a child purely by current_program_state
;;;     identity; the slot_a fallback in kernel_yield is reached only
;;;     after both children EXIT, and at that point any state value
;;;     in slot_a is fine.
;;;
;;; Returns when both pipeline children have exited and kernel_yield's
;;; fallback path resumes slot_a's saved ESP — that lands here right
;;; after the `mov esp, saved_esp; ret` sequence in the resume path.
;;;
;;; Clobbers: EAX, EDX (same shape as kernel_yield).
;;; -----------------------------------------------------------------------
kernel_yield_to_pipeline_start:
        cli
        ;; pushad mirrors kernel_yield's prologue so that the eventual
        ;; slot_a-resume (kernel_yield's .resume_slot path after both
        ;; children exit) can popad-restore slot_a's GP regs.  Without
        ;; this, kernel_yield's popad would restore garbage off whatever
        ;; happened to be at the parked slot_a ESP.
        pushad
        ;; Save the shell's (slot_a's) kernel ESP (pointing into the
        ;; pushad block just pushed).
        mov edx, [current_program_state]
        mov [edx + PROGRAM_STATE_OFFSET_SAVED_ESP], esp
        ;; Pick the first runnable child.  sys_pipeline2 set both
        ;; program_state_b.state and program_state_c.state to
        ;; STATE_RUNNING before calling here, so at least one is
        ;; STATE_RUNNING.  Prefer slot_b so cmd1 runs first.
        mov edx, program_state_b
        cmp byte [edx + PROGRAM_STATE_OFFSET_STATE], STATE_RUNNING
        je .pipeline_start_resume
        mov edx, program_state_c
.pipeline_start_resume:
        mov [current_program_state], edx
        mov eax, [edx + PROGRAM_STATE_OFFSET_PD_PHYS]
        mov cr3, eax
        mov esp, [edx + PROGRAM_STATE_OFFSET_SAVED_ESP]
        call tss_set_esp0_for_current_slot
        sti
        popad                  ; the first child's pushad block (primed by
                               ; build_initial_iret_frame) lands here on
                               ; first run; on subsequent yields the
                               ; resuming slot's own pushad is restored.
        ret

;;; -----------------------------------------------------------------------
;;; stage_user_argv
;;;
;;; Write the Linux SysV i386 startup frame onto the topmost page of the
;;; new program's user stack and record the resulting initial ESP into
;;; [current_program_state + PROGRAM_STATE_OFFSET_INITIAL_ESP].
;;;
;;; Final user-stack layout at process entry (high-to-low addresses
;;; growing down toward initial_esp):
;;;
;;;   USER_STACK_TOP  -> (one past last writable byte)
;;;   ... argv[0..argc-1] NUL-terminated string bytes (packed high) ...
;;;   ... padding to 16-byte alignment of the pointer-array start ...
;;;   NULL                              <-- envp[0] terminator
;;;   NULL                              <-- argv[argc] terminator
;;;   argv[argc-1] pointer (user-virt)
;;;   ...
;;;   argv[0] pointer (user-virt)
;;;   argc                              <-- initial_esp
;;;
;;; A program reads `argc` from `[esp]` and `argv` from `lea r,[esp+4]`.
;;; `envp` (always empty here) sits at `[esp + 4*(argc+1) + 4]`.
;;;
;;; The user argv strings are read **directly** from the caller's PD —
;;; sys_exec / sys_pipeline2 keep the parent's PD as the active CR3 from
;;; entry through child build, so [esi + i*4] (the argv pointer slot) and
;;; the strings themselves resolve under the parent's address space.  The
;;; writes go through `kmap_map` on the child's stack frame, which lives
;;; in the kernel half (shared across every PD).  No kernel scratch is
;;; involved — the bytes flow shell PD → kmap kvirt in one pass.
;;;
;;; Inputs:  [pending_argv_user_ptr] = user-virt char** (0 = no args).
;;;                                    Must have been validated by
;;;                                    .validate_user_argv (syscall.asm)
;;;                                    before this point.
;;;          Active CR3 = caller's PD (so user-virt reads resolve).
;;;          [topmost_stack_frame_phys] = phys of the user stack page
;;;                                    that contains USER_STACK_TOP-1
;;;                                    (mapped at user-virt 0xFF7FF000).
;;;                                    Captured by build_child_program_state's
;;;                                    stack-mapping loop.
;;;
;;; Output:  [current_program_state + INITIAL_ESP] = user-virt ESP for
;;;          the first iretd into ring 3.
;;;
;;; Clobbers: EAX, EBX, ECX, EDX, ESI, EDI, EBP.
;;; -----------------------------------------------------------------------
stage_user_argv:
        ;; Local frame (referenced via EBP-relative addressing so the
        ;; push loop in step 1 doesn't shift the offsets):
        ;;   [ebp -  4]  kvirt        — kmap alias of the child's
        ;;                              topmost stack frame
        ;;   [ebp -  8]  argc         — final argv count
        ;;   [ebp - 12]  cursor       — in-frame byte offset of the
        ;;                              next byte to write (descends
        ;;                              from 0x1000 toward 0)
        ;;   [ebp - 16]  argv_base    — saved [pending_argv_user_ptr]
        push ebp
        mov  ebp, esp
        sub  esp, 16
        push ebx
        push esi
        push edi

        mov  eax, [topmost_stack_frame_phys]
        call kmap_map                           ; EAX = kvirt
        mov  [ebp - 4], eax
        mov  dword [ebp - 12], 0x1000

        ;; --- Count argv entries (capped at MAX_ARGV_ENTRIES; pre-
        ;;     validated by .validate_user_argv before we got here).
        mov  eax, [pending_argv_user_ptr]
        mov  [ebp - 16], eax
        test eax, eax
        jz   .argc_zero
        mov  esi, eax
        xor  ebx, ebx
.count_loop:
        cmp  ebx, MAX_ARGV_ENTRIES
        jae  .have_argc
        mov  edx, [esi + ebx*4]
        test edx, edx
        jz   .have_argc
        inc  ebx
        jmp  .count_loop
.argc_zero:
        xor  ebx, ebx
.have_argc:
        mov  [ebp - 8], ebx
        test ebx, ebx
        jz   .strings_done

        ;; --- Step 1: walk argv forward (i = 0..argc-1), measure each
        ;;     string under the caller's PD, reserve (len+1) bytes at
        ;;     the top of the in-frame cursor, copy directly into the
        ;;     kmap alias, and push the resulting child-side user-virt
        ;;     onto the kernel stack.  After this loop the kernel
        ;;     stack contains, top-down:
        ;;        [esp]                 argv[argc-1] uvirt
        ;;        [esp + 4]             argv[argc-2] uvirt
        ;;        ...
        ;;        [esp + 4*(argc-1)]    argv[0] uvirt
        ;;     The pop loop below consumes these in argc-1..0 order,
        ;;     descending the cursor → the child reads argv[0] at the
        ;;     lowest address, argv[argc-1] at the highest, matching
        ;;     the SysV layout.
        xor  ebx, ebx                           ; i = 0
.string_loop:
        cmp  ebx, [ebp - 8]
        jae  .strings_done

        mov  esi, [ebp - 16]
        mov  eax, [esi + ebx*4]                 ; user_ptr (caller PD)
        mov  esi, eax                           ; ESI = source for rep movsb below

        ;; strlen capped at ARG_MAX; EDI = length excluding NUL.
        xor  edi, edi
.strlen_loop:
        cmp  edi, ARG_MAX
        jae  .strlen_done
        cmp  byte [esi + edi], 0
        je   .strlen_done
        inc  edi
        jmp  .strlen_loop
.strlen_done:
        inc  edi                                ; +1 for the NUL byte

        ;; cursor -= byte_count.
        mov  ecx, [ebp - 12]
        sub  ecx, edi
        mov  [ebp - 12], ecx

        ;; rep movsb: ESI = user source (set), EDI = kvirt dest,
        ;; ECX = byte count.
        mov  ecx, edi                           ; count
        mov  edi, [ebp - 4]                     ; kvirt
        add  edi, [ebp - 12]                    ; + cursor
        cld
        rep  movsb

        ;; Stash the child-side user-virt of this string.
        mov  eax, [ebp - 12]
        add  eax, STACK_VIRT_END - 0x1000
        push eax

        inc  ebx
        jmp  .string_loop
.strings_done:

        ;; --- Step 2: 16-byte align the cursor down.  USER_STACK_TOP
        ;;     and 0x1000 are both 16-byte aligned, so the resulting
        ;;     user-virt is too.
        mov  ecx, [ebp - 12]
        and  ecx, ~0x0F

        ;; --- Step 3: envp[0] = NULL (empty envp, reserved slot).
        sub  ecx, 4
        mov  eax, [ebp - 4]                     ; kvirt (reused across all writes below)
        mov  dword [eax + ecx], 0

        ;; --- Step 4: argv[argc] = NULL.
        sub  ecx, 4
        mov  dword [eax + ecx], 0

        ;; --- Step 5: pop argc child-side user-virts off the kernel
        ;;     stack into argv[argc-1..0] (cursor descending).
        mov  edx, [ebp - 8]
        test edx, edx
        jz   .ptrs_done
.ptr_loop:
        sub  ecx, 4
        pop  edi                                ; child-side user-virt
        mov  [eax + ecx], edi
        dec  edx
        jnz  .ptr_loop
.ptrs_done:

        ;; --- Step 6: argc at the bottom of the frame.
        sub  ecx, 4
        mov  edx, [ebp - 8]
        mov  [eax + ecx], edx

        ;; --- Step 7: record initial_esp into the slot.
        lea  eax, [ecx + STACK_VIRT_END - 0x1000]
        mov  edx, [current_program_state]
        mov  [edx + PROGRAM_STATE_OFFSET_INITIAL_ESP], eax

        ;; --- Step 8: kmap_unmap and return.
        mov  eax, [ebp - 4]                     ; kvirt
        call kmap_unmap

        pop  edi
        pop  esi
        pop  ebx
        mov  esp, ebp
        pop  ebp
        ret

;;; -----------------------------------------------------------------------
;;; tss_set_esp0_for_current_slot — update TSS.ESP0 to point at the
;;; current_program_state's per-slot kernel stack top.  Called before
;;; every iretd-to-userland so the next ring-3-to-ring-0 transition
;;; lands on the correct slot's kernel stack.
;;;
;;; Preserves all GP registers: callers invoke this between popad and
;;; iretd in the syscall-return / child-terminate / spawn-failed paths,
;;; where the GP regs already hold the user's restored values.
;;;
;;; Out: TSS.ESP0 updated; all registers preserved.
;;; -----------------------------------------------------------------------
tss_set_esp0_for_current_slot:
        push eax
        push edx
        mov edx, [current_program_state]
        mov eax, [edx + PROGRAM_STATE_OFFSET_KERNEL_STACK_TOP]
        mov [tss_data + 4], eax
        pop edx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; userland_entry_stub — used to prime a never-run slot's kernel
;;; stack.  When a slot is first scheduled via kernel_yield, the
;;; saved_esp it loads points at a stack where:
;;;   [esp]      = userland_entry_stub  (this label's address)
;;;   [esp+4]    = edi    \
;;;   [esp+8]    = esi    |
;;;   [esp+12]   = ebp    | pushad area (8 dwords)
;;;   [esp+16]   = esp    |
;;;   [esp+20]   = ebx    |
;;;   [esp+24]   = edx    |
;;;   [esp+28]   = ecx    |
;;;   [esp+32]   = eax    /
;;;   [esp+36]   = eip    \
;;;   [esp+40]   = cs     | iret frame (5 dwords)
;;;   [esp+44]   = eflags |
;;;   [esp+48]   = esp    |
;;;   [esp+52]   = ss     /
;;;
;;; kernel_yield's ret pops `userland_entry_stub` into EIP, leaving the
;;; pushad+iret frame at [esp..esp+52).  popad+iretd then drops to user.
;;; -----------------------------------------------------------------------
userland_entry_stub:
        popad
        iretd

libbboeos_install:
        ;; Read `lib/libbboeos` from the disk image, copy it into one or
        ;; more freshly-allocated frames, and stash the phys array for
        ;; build_child_program_state to map (with PTE_SHARED) at
        ;; consecutive user-virts starting at FUNCTION_TABLE (0x10000) in
        ;; every per-program PD.  The file's on-disk layout matches the
        ;; in-memory page sequence: page 0 carries helper bodies /
        ;; sigreturn / zero pad / FUNCTION_POINTER_TABLE at offset 0x800;
        ;; pages 1..N-1 (if present) hold whatever spills past 4 KB.
        ;; Streaming one sector at a time mirrors program_enter's
        ;; binary-page loop; each frame is zero-filled first so any
        ;; region the file doesn't cover (e.g. the helpers/pointer-table
        ;; gap in page 0, or the tail of the final page beyond
        ;; libbboeos's end) stays clean.
        ;;
        ;; Caller invariant: vfs_init must have run already so
        ;; vfs_find / vfs_read_sec resolve.  protected_mode_entry
        ;; orders the boot calls accordingly.
        pushad

        ;; Look up the on-disk file.  vfs_find populates vfs_found_*
        ;; with the file's start sector, size, type, and dir-entry
        ;; coordinates.  CF set = missing / unreadable, which is
        ;; fatal at boot.
        mov esi, libbboeos_path
        call vfs_find
        jc .panic

        ;; Compute page count = ceil(size / 4096) and assert it fits
        ;; inside the compile-time LIBBBOEOS_PAGE_COUNT_MAX ceiling.  A
        ;; too-big libbboeos here means either libbboeos has grown past
        ;; the kernel's BSS-sized phys-frame array (bump
        ;; LIBBBOEOS_PAGE_COUNT_MAX) or something else is writing to the
        ;; file (bug).  Either way it can't silently truncate at boot.
        mov eax, [vfs_found_size]
        add eax, 0xFFF
        shr eax, 12                     ; EAX = ceil(size / 4096)
        cmp eax, LIBBBOEOS_PAGE_COUNT_MAX
        ja .panic
        mov [libbboeos_page_count], eax

        ;; Build a private fd struct that vfs_read_sec can drive.
        ;; Mirrors program_enter's program_fd usage; libbboeos_install runs
        ;; once at boot before any program loads, so we can safely
        ;; co-opt the same scratch struct here.
        mov edi, program_fd
        mov ecx, FD_ENTRY_SIZE / 4
        xor eax, eax
        cld
        rep stosd
        mov al, [vfs_found_type]
        mov [program_fd + FD_OFFSET_TYPE], al
        mov ax, [vfs_found_inode]
        mov [program_fd + FD_OFFSET_START], ax
        mov eax, [vfs_found_size]
        mov [program_fd + FD_OFFSET_SIZE], eax
        mov ax, [vfs_found_dir_sec]
        mov [program_fd + FD_OFFSET_DIRECTORY_SECTOR], ax
        mov ax, [vfs_found_dir_off]
        mov [program_fd + FD_OFFSET_DIRECTORY_OFFSET], ax

        ;; Per-page loop: allocate frame, kmap it, zero it, read up to
        ;; 8 sectors of file data into it, unmap.  EBP holds page index;
        ;; each iteration stashes the frame's phys at
        ;; libbboeos_code_phys[page_index].
        xor ebp, ebp                    ; page index
.page_loop:
        cmp ebp, [libbboeos_page_count]
        jae .page_done

        call frame_alloc
        jc .panic
        mov ebx, ebp
        shl ebx, 2
        mov [libbboeos_code_phys + ebx], eax
        call kmap_map                   ; EAX = kvirt
        mov edi, eax                    ; EDI = kvirt (held across the streaming loop)

        ;; Zero the entire frame so any bytes the file doesn't cover
        ;; — the helpers/pointer-table gap in page 0, or the tail of
        ;; the final page — stay zero.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi

        push edi                        ; save frame kvirt for kmap_unmap
        xor edx, edx                    ; sector-in-page index
.sector_loop:
        cmp edx, 8
        jae .sector_done
        ;; File offset for this (page_index, sector_in_page) pair:
        ;; file_offset = page_index * 4096 + sector_in_page * 512.
        mov eax, ebp
        shl eax, 12
        mov ecx, edx
        shl ecx, 9
        add eax, ecx                    ; EAX = file_offset
        cmp eax, [vfs_found_size]
        jae .sector_done                ; past end of file

        mov [program_fd + FD_OFFSET_POSITION], eax

        push edx
        push edi
        mov esi, program_fd
        call vfs_read_sec
        pop edi
        pop edx
        jc .panic

        ;; Copy min(size - file_offset, 512) bytes from sector_buffer
        ;; to (frame + sector_in_page * 512).
        mov eax, ebp
        shl eax, 12
        mov ecx, edx
        shl ecx, 9
        add eax, ecx                    ; EAX = file_offset
        mov ebx, [vfs_found_size]
        sub ebx, eax                    ; EBX = bytes remaining in file
        cmp ebx, 512
        jbe .chunk_ready
        mov ebx, 512
.chunk_ready:
        push esi
        push edi
        push edx
        push ecx
        mov esi, [sector_buffer]
        mov ecx, edx
        shl ecx, 9
        add edi, ecx                    ; EDI = frame + sector_in_page*512
        mov ecx, ebx
        cld
        rep movsb
        pop ecx
        pop edx
        pop edi
        pop esi

        inc edx
        jmp .sector_loop
.sector_done:
        pop eax                         ; frame kvirt
        call kmap_unmap
        inc ebp
        jmp .page_loop
.page_done:
        popad
        ret
.panic:
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

libbboeos_path  db "lib/libbboeos", 0

        ;; Per-program-load state used by program_enter.
        ;; current_program_state is pre-initialised to program_state_a (in
        ;; BSS) so the PIT handler is safe before shell_reload runs;
        ;; shell_reload also sets it (redundant but harmless).  The pointer
        ;; itself stays in .text because it has a non-zero initializer; the
        ;; PROGRAM_STATE slots and the other per-load scalars live in BSS
        ;; (see kernel.asm's section .bss).
current_program_state   dd program_state_a  ; pointer to the running program's PROGRAM_STATE slot

shell_path      db "bin/shell", 0

welcome_msg     db "Welcome to BBoeOS!", 13, 10, "Version 0.11.0 (2026/05/10)", 13, 10, 0

;;; -----------------------------------------------------------------------
