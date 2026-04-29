;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; protected_mode_entry runs once per boot — TSS / IRQ install, vDSO +
;;; JUMP_TABLE shared-frame allocation, driver / VFS / NIC init, banner
;;; — then falls through into shell_reload.
;;;
;;; shell_reload is the re-entry point for SYS_EXIT (after the dying
;;; program's PD has been torn down by sys_exit).  It loads bin/shell
;;; off disk into a kernel-side scratch buffer, zeroes the BUFFER /
;;; EXEC_ARG snapshots (a fresh shell inherits no args), and `jmp`s
;;; program_enter.
;;;
;;; program_enter builds a fresh per-program PD via address_space_create,
;;; populates the user-visible regions (program text + BSS, vDSO code
;;; page, user stack, shared JUMP_TABLE region), restores BUFFER /
;;; EXEC_ARG into the new program's first user frame, snapshots the
;;; kernel ESP for sys_exit, switches CR3, and `iretd`s at CPL=3.
;;;
;;; Any CPU exception fired past this point vectors through `idt.asm`'s
;;; `exc_common` and prints `EXCnn` on COM1.  Phase 5 will give
;;; user-mode #PF / #GP a kill-program path; today they still halt.
;;; ------------------------------------------------------------------------

        PMODE_IRQ0_VECTOR       equ 0x20        ; matches the pic_remap master base
        PMODE_IRQ6_VECTOR       equ 0x26

        ;; User-page PTE flag bundles.
        PTE_USER_RW             equ 0x107       ; P | RW | U
        PTE_USER_RX             equ 0x105       ; P | U (read-only — no NX in 32-bit non-PAE)
        PTE_USER_RX_SHARED      equ (PTE_USER_RX | ADDRESS_SPACE_PTE_SHARED)
        PTE_USER_RW_SHARED      equ (PTE_USER_RW | ADDRESS_SPACE_PTE_SHARED)

        ;; User address-space layout (legacy PROGRAM_BASE = 0x600):
        ;;   PTE 0x000          : private — EXEC_ARG, BUFFER, prog prefix
        ;;   PTEs 0x001..       : private — program text + BSS
        ;;   PTE 0x010          : shared  — vDSO code page (R-X)
        ;;   PTEs 0x080..0x08F  : private — user stack (16 × 4 KB = 64 KB)
        ;;   PTEs 0x300..0x3FF  : shared  — asm.c JUMP_TABLE (256 × 4 KB)
        VDSO_VIRT               equ FUNCTION_TABLE          ; 0x00010000
        STACK_VIRT_BASE         equ 0x80000
        STACK_VIRT_END          equ 0x90000                 ; one past last page
        JUMP_TABLE_VIRT_BASE    equ 0x300000
        JUMP_TABLE_FRAME_COUNT  equ 256

pmode_irq0_handler:
        ;; PIT tick.  Increment `system_ticks` (dword in rtc.asm's
        ;; data region, reachable via flat DS), EOI the master PIC,
        ;; iretd.  Interrupt gate entry leaves IF=0 for the body, so
        ;; the `inc dword [mem]` is safe against reentrancy; on a
        ;; single CPU we don't need the LOCK prefix.
        push eax
        inc dword [system_ticks]
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        pop eax
        iretd

pmode_irq6_handler:
        ;; FDC command complete.  EOI.
        push eax
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        pop eax
        iretd

;;; -----------------------------------------------------------------------
;;; program_enter
;;;
;;; Builds the per-program PD and `iretd`s into ring 3.  Caller
;;; invariants:
;;;   * Active PD = `kernel_pd_template` (no user mappings).
;;;   * Program binary staged at `program_scratch`; `vfs_found_size`
;;;     holds the binary length.
;;;   * `buffer_snapshot` (256 B) and `exec_arg_snapshot` (4 B) hold
;;;     the BUFFER / EXEC_ARG content the new program inherits.
;;;
;;; Never returns.  On panic (allocator OOM during PD build) the kernel
;;; halts — there's no graceful recovery for "ran out of frames mid-
;;; program-load" yet.
;;; -----------------------------------------------------------------------
program_enter:
        call fd_init

        ;; --- Allocate fresh PD ---
        call address_space_create
        jc .panic
        mov [current_pd_phys], eax

        ;; --- Determine total user image size ---
        ;; binsize = vfs_found_size; bsssize from trailer at end of
        ;; program_scratch (matches the PR-#234 6-byte trailer or the
        ;; legacy 4-byte trailer).  total = PROGRAM_BASE + binsize +
        ;; bsssize, page-aligned up.
        movzx ecx, word [vfs_found_size]
        mov edi, program_scratch
        add edi, ecx                        ; EDI = end of binary in scratch
        xor eax, eax                        ; default bss_size = 0
        cmp ecx, 6
        jb .check_old_trailer
        cmp word [edi - 2], BSS_MAGIC32
        jne .check_old_trailer
        mov eax, [edi - 6]
        jmp .have_bss_size
.check_old_trailer:
        cmp ecx, 4
        jb .have_bss_size
        cmp word [edi - 2], BSS_MAGIC
        jne .have_bss_size
        movzx eax, word [edi - 4]
.have_bss_size:
        add eax, ecx
        add eax, PROGRAM_BASE
        add eax, 0xFFF
        and eax, 0xFFFFF000
        mov [user_image_end], eax

        ;; --- Map low frame (virt 0x000-0x0FFF) ---
        ;; Contains EXEC_ARG (4 B at 0x4FC), BUFFER (256 B at 0x500),
        ;; and the program prefix (up to 0xA00 B at 0x600..0xFFF).
        call frame_alloc
        jc .panic
        push eax                            ; [esp+0] = low frame phys
        mov edi, eax
        add edi, 0xC0000000                 ; kernel-virt of frame
        ;; Zero entire frame.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        ;; EXEC_ARG snapshot.
        mov eax, [exec_arg_snapshot]
        mov [edi + EXEC_ARG], eax
        ;; BUFFER snapshot.
        push edi
        mov esi, buffer_snapshot
        lea edi, [edi + BUFFER]
        mov ecx, MAX_INPUT / 4
        rep movsd
        pop edi
        ;; Program prefix (min(binsize, 0x1000-PROGRAM_BASE) bytes).
        mov esi, program_scratch
        movzx ecx, word [vfs_found_size]
        cmp ecx, 0x1000 - PROGRAM_BASE      ; 0xA00
        jbe .copy_low_partial
        mov ecx, 0x1000 - PROGRAM_BASE
.copy_low_partial:
        push edi
        lea edi, [edi + PROGRAM_BASE]
        rep movsb
        pop edi
        ;; Map low frame at user-virt 0.
        pop ecx                             ; low frame phys
        mov eax, [current_pd_phys]
        xor ebx, ebx
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic

        ;; --- Map remaining program text + BSS frames ---
        mov dword [virt_cursor], 0x1000
.prog_page_loop:
        mov eax, [virt_cursor]
        cmp eax, [user_image_end]
        jae .prog_pages_done
        ;; Allocate frame.
        call frame_alloc
        jc .panic
        push eax                            ; frame phys
        mov edi, eax
        add edi, 0xC0000000                 ; EDI = kernel-virt
        ;; Zero frame.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi                             ; restore EDI = frame start
        ;; Copy program bytes if this page is within the binary.
        mov eax, [virt_cursor]
        sub eax, PROGRAM_BASE               ; scratch offset for this page
        movzx ecx, word [vfs_found_size]
        cmp eax, ecx
        jae .prog_page_no_copy              ; past binary end → BSS-only, leave zeroed
        sub ecx, eax                        ; bytes remaining in binary
        cmp ecx, 0x1000
        jbe .prog_page_copy
        mov ecx, 0x1000
.prog_page_copy:
        mov esi, program_scratch
        add esi, eax                        ; ESI = scratch source
        rep movsb
.prog_page_no_copy:
        ;; Map frame at virt_cursor.
        pop ecx                             ; frame phys
        mov eax, [current_pd_phys]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic
        add dword [virt_cursor], 0x1000
        jmp .prog_page_loop
.prog_pages_done:

        ;; --- Map vDSO code page (shared, R-X user) ---
        mov eax, [current_pd_phys]
        mov ebx, VDSO_VIRT
        mov ecx, [vdso_code_phys]
        mov edx, PTE_USER_RX_SHARED
        call address_space_map_page
        jc .panic

        ;; --- Map user stack (private, 16 frames, zeroed) ---
        mov dword [virt_cursor], STACK_VIRT_BASE
.stack_page_loop:
        mov eax, [virt_cursor]
        cmp eax, STACK_VIRT_END
        jae .stack_pages_done
        call frame_alloc
        jc .panic
        push eax
        mov edi, eax
        add edi, 0xC0000000
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop ecx
        mov eax, [current_pd_phys]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic
        add dword [virt_cursor], 0x1000
        jmp .stack_page_loop
.stack_pages_done:

        ;; --- Map shared JUMP_TABLE pages ---
        ;; Frames allocated at boot in `jump_table_setup` and stored in
        ;; `jump_table_frames[256]`; mapped here with PTE_SHARED so
        ;; address_space_destroy doesn't free them.  asm.c writes its
        ;; symbol/jump tables here; other programs ignore the region.
        xor esi, esi                        ; ESI = frame index 0..255
.jt_page_loop:
        cmp esi, JUMP_TABLE_FRAME_COUNT
        jae .jt_pages_done
        mov ebx, esi
        shl ebx, 12
        add ebx, JUMP_TABLE_VIRT_BASE       ; user-virt
        mov ecx, [jump_table_frames + esi*4]
        mov edx, PTE_USER_RW_SHARED
        mov eax, [current_pd_phys]
        call address_space_map_page
        jc .panic
        inc esi
        jmp .jt_page_loop
.jt_pages_done:

        ;; --- Snapshot kernel ESP for sys_exit ---
        mov [shell_esp], esp

        ;; --- Switch to the new PD ---
        mov eax, [current_pd_phys]
        mov cr3, eax

        ;; --- iretd into ring 3 ---
        ;; Reload data segments to USER_DATA_SELECTOR before the iretd
        ;; (iretd doesn't reload DS/ES/FS/GS).  CPL=0 can still
        ;; read/write through those selectors because CPL ≤ DPL on
        ;; access.
        mov ax, USER_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov fs, ax
        mov gs, ax
        push dword USER_DATA_SELECTOR
        push dword USER_STACK_TOP
        push dword 0x202
        push dword USER_CODE_SELECTOR
        push dword PROGRAM_BASE
        iretd

.panic:
        ;; Allocator OOM during program load.  No graceful recovery
        ;; yet — halt with '!' on COM1 (matches high_entry's panic).
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

protected_mode_entry:
        ;; Segment registers, ESP, GDTR, and IDTR are already in place
        ;; — `high_entry` (kernel.asm) ran first and handed off here
        ;; with the kernel GDT / IDT live and ESP pointing at
        ;; `kernel_stack_top`.  We patch the TSS, ltr, bring up devices,
        ;; allocate shared user-page frames (vDSO + JUMP_TABLE), and
        ;; drop into shell_reload.
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

        ;; Reprogram PIT to 100 Hz (MS_PER_TICK=10 ms/tick).
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

        ;; Allocate shared user-page frames for the vDSO code page and
        ;; the JUMP_TABLE region.  Both are mapped (with PTE_SHARED)
        ;; into every per-program PD by program_enter; address_space_
        ;; destroy skips them on teardown.
        call vdso_install
        call jump_table_setup

        call ata_init
        call fd_init
        call fdc_init
        call ps2_init
        call vfs_init
        ;; Probe the NE2000 NIC and bring it up if present.  CF set =
        ;; no NIC, which is fine — netinit / net programs surface that
        ;; via a "no NIC" message rather than halting the kernel.
        call network_initialize

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
        ;; Active PD: kernel_pd_template (sys_exit just destroyed the
        ;; dying program's PD and switched off it, or this is the first
        ;; boot and CR3 was set up by high_entry).  Load bin/shell
        ;; into program_scratch, reset the BUFFER / EXEC_ARG snapshots,
        ;; and `jmp` program_enter to build the shell's PD.
        mov esi, shell_path
        call vfs_find
        jc .shell_fail
        mov edi, program_scratch
        call vfs_load
        jc .shell_fail
        ;; Fresh shell inherits no args.  Zero both snapshots.
        mov edi, buffer_snapshot
        mov ecx, MAX_INPUT / 4
        xor eax, eax
        cld
        rep stosd
        mov dword [exec_arg_snapshot], 0
        jmp program_enter

        .shell_fail:
        ;; Missing or unreadable shell.  Halt — no recovery here.
        cli
        hlt
        jmp $-1

vdso_install:
        ;; Allocate one frame for the vDSO code page and copy
        ;; `vdso_image` (the embedded blob from kernel.asm) into it via
        ;; the kernel direct map.  program_enter installs the frame at
        ;; user-virt FUNCTION_TABLE (0x10000) in every per-program PD
        ;; with PTE_SHARED, so user programs see the vDSO and
        ;; address_space_destroy never frees the frame.
        push eax
        push ecx
        push esi
        push edi
        call frame_alloc
        jc .panic
        mov [vdso_code_phys], eax
        mov edi, eax
        add edi, 0xC0000000             ; direct-map kernel-virt of frame
        mov esi, vdso_image
        mov ecx, (vdso_image_end - vdso_image) / 4
        cld
        rep movsd
        pop edi
        pop esi
        pop ecx
        pop eax
        ret
.panic:
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

jump_table_setup:
        ;; Allocate JUMP_TABLE_FRAME_COUNT frames and stash their phys
        ;; addresses in `jump_table_frames[]`.  Each per-program PD
        ;; maps these into PTEs 0x300..0x3FF with PTE_SHARED, giving
        ;; asm.c the 1 MB user-virt scratch region it expects at
        ;; SYMBOL_BASE / JUMP_TABLE.  Frames are zero-initialised
        ;; because frame_alloc returns from a bitmap; user code is
        ;; expected to own all writes there.  Residue across program
        ;; runs is fine for asm.c's two-pass approach (pass 1 fully
        ;; rewrites pass-1's table from the source).
        push eax
        push ebx
        push ecx
        push edi
        xor ecx, ecx
        mov edi, jump_table_frames
.alloc_loop:
        cmp ecx, JUMP_TABLE_FRAME_COUNT
        jae .done
        push ecx
        push edi
        call frame_alloc
        pop edi
        pop ecx
        jc .panic
        mov [edi], eax
        add edi, 4
        ;; Zero the frame so JUMP_TABLE start state is well-defined.
        push ecx
        push edi
        mov edi, eax
        add edi, 0xC0000000
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        pop ecx
        inc ecx
        jmp .alloc_loop
.done:
        pop edi
        pop ecx
        pop ebx
        pop eax
        ret
.panic:
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

        ;; Physical address of `kernel_pd_template`, the page directory
        ;; whose top-256 PDEs are copied into every per-program PD as
        ;; the kernel half of the address space.  Phase 3 promotes the
        ;; boot PD into this slot (= 0x1000) and stops here; Phase 4's
        ;; per-address-space work consumes it from `address_space_create`.
kernel_pd_template_phys dd 0

        ;; Per-program-load state used by program_enter.
current_pd_phys         dd 0    ; new PD being built
user_image_end          dd 0    ; PROGRAM_BASE + binsize + bsssize, page-aligned up
virt_cursor             dd 0    ; current user-virt during page-walk loops
vdso_code_phys          dd 0    ; phys of the shared vDSO code frame

shell_esp       dd 0            ; kernel ESP snapshot, restored by sys_exit
shell_path      db "bin/shell", 0

        ;; BUFFER / EXEC_ARG cross-AS snapshot.  sys_exec writes these
        ;; from the dying shell's user pages BEFORE address_space_destroy;
        ;; program_enter copies them into the new program's first user
        ;; frame at user-virt 0x500 / 0x4FC.  shell_reload zeroes them
        ;; (a fresh shell inherits no args).
        align 4
buffer_snapshot         times MAX_INPUT db 0
exec_arg_snapshot       dd 0

        ;; JUMP_TABLE shared frames.  Allocated once at boot in
        ;; `jump_table_setup`; mapped into every per-program PD by
        ;; program_enter with PTE_SHARED so they survive teardown.
        align 4
jump_table_frames       times JUMP_TABLE_FRAME_COUNT dd 0

        ;; 32-bit TSS.  Only SS0/ESP0/IOPB-offset are populated (in
        ;; protected_mode_entry); all other fields stay zero because we
        ;; don't use hardware task switching.  Sized to the 104-byte
        ;; standard layout so the IOPB-past-limit trick parks I/O.
        align 4
tss_data:
        times 104 db 0

welcome_msg     db "Welcome to BBoeOS!", 13, 10, "Version 0.8.1 (2026/04/28)", 13, 10, 0

;;; -----------------------------------------------------------------------
